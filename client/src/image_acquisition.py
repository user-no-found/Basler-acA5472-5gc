# -*- coding: utf-8 -*-
"""
图像采集模块

负责连续图像采集，支持录像和预览功能
使用pypylon SDK进行相机采集
预览功能使用Pillow进行图像缩放和JPEG编码

性能优化:
- 使用GrabStrategy_LatestImageOnly避免帧堆积
- 支持跳帧策略（网络拥塞时跳过帧）
- 动态JPEG质量调整（根据带宽调整）
- 预分配缓冲区减少内存分配

异常处理:
- 采集超时处理（返回错误码0x0103）
- 相机断线检测（返回错误码0x0105）
- JPEG编码失败处理（返回错误码0x0501）
- 资源清理保证（finally块）
"""
import threading
import time
import queue
import io
from typing import Optional, Callable, Tuple, TYPE_CHECKING
from dataclasses import dataclass
from enum import Enum

import numpy as np
from loguru import logger

try:
    from pypylon import pylon
    PYPYLON_AVAILABLE = True
except ImportError:
    PYPYLON_AVAILABLE = False
    logger.warning("pypylon未安装，图像采集功能不可用")

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    logger.warning("Pillow未安装，预览缩放功能受限")

#导入性能优化工具
try:
    from .utils.performance import (
        ImageBufferPool,
        CongestionDetector,
        PerformanceMonitor,
        fast_resize_nearest,
    )
    PERFORMANCE_UTILS_AVAILABLE = True
except ImportError:
    PERFORMANCE_UTILS_AVAILABLE = False
    logger.debug("性能优化工具未加载")

#导入错误码
try:
    from .utils.errors import ErrorCode
except ImportError:
    #独立运行时的兼容处理
    class ErrorCode:
        CAMERA_NOT_CONNECTED = 0x0101
        CAMERA_GRAB_TIMEOUT = 0x0103
        CAMERA_DISCONNECTED = 0x0105
        JPEG_ENCODE_FAILED = 0x0501
        STATE_RECORDING = 0x0301
        PREVIEW_ALREADY_STARTED = 0x0304


class AcquisitionMode(Enum):
    """采集模式枚举"""
    IDLE = 0        #空闲
    RECORDING = 1   #录像中
    PREVIEW = 2     #预览中


@dataclass
class AcquisitionConfig:
    """采集配置"""
    fps: int = 5                    #帧率
    resolution: Tuple[int, int] = (1920, 1080)  #分辨率
    duration: int = 0               #录像时长（秒），0表示手动停止
    buffer_size: int = 30           #缓冲队列大小


class ImageAcquisition:
    """
    图像采集器

    提供连续采集功能，支持录像和预览模式

    异常处理:
    - 采集超时: 记录警告，继续尝试
    - 相机断线: 检测并触发错误回调
    - 回调异常: 捕获并记录，不影响采集循环
    """

    #分辨率索引映射
    RESOLUTION_MAP = {
        0: (1920, 1080),
        1: (1280, 720),
        2: (640, 480),
    }

    #最大连续超时次数
    MAX_TIMEOUT_COUNT = 10

    def __init__(self, camera_controller=None):
        """
        初始化图像采集器

        Args:
            camera_controller: 相机控制器实例
        """
        self._camera = camera_controller
        self._mode = AcquisitionMode.IDLE
        self._config = AcquisitionConfig()

        #采集线程
        self._acquisition_thread: Optional[threading.Thread] = None
        self._running = False

        #图像缓冲队列
        self._frame_queue: queue.Queue = queue.Queue(maxsize=30)

        #回调函数
        self._frame_callback: Optional[Callable[[np.ndarray, int], None]] = None
        self._complete_callback: Optional[Callable[[], None]] = None
        self._error_callback: Optional[Callable[[int, str], None]] = None  #错误回调

        #统计信息
        self._frame_count = 0
        self._start_time = 0.0
        self._actual_fps = 0.0
        self._timeout_count = 0  #连续超时计数
        self._last_error_code: Optional[int] = None

        #线程锁
        self._lock = threading.RLock()

        logger.info("图像采集器初始化完成")

    @property
    def mode(self) -> AcquisitionMode:
        """获取当前采集模式"""
        return self._mode

    @property
    def is_running(self) -> bool:
        """是否正在采集"""
        return self._running and self._mode != AcquisitionMode.IDLE

    @property
    def frame_count(self) -> int:
        """获取已采集帧数"""
        return self._frame_count

    @property
    def actual_fps(self) -> float:
        """获取实际帧率"""
        return self._actual_fps

    @property
    def last_error_code(self) -> Optional[int]:
        """获取最后一次错误码"""
        return self._last_error_code

    def set_error_callback(self, callback: Callable[[int, str], None]) -> None:
        """
        设置错误回调函数

        Args:
            callback: 回调函数，参数为(错误码, 错误描述)
        """
        self._error_callback = callback

    def _report_error(self, error_code: int, description: str) -> None:
        """
        报告错误

        Args:
            error_code: 错误码
            description: 错误描述
        """
        self._last_error_code = error_code
        logger.error(f"采集错误[0x{error_code:04X}]: {description}")
        if self._error_callback:
            try:
                self._error_callback(error_code, description)
            except Exception as e:
                logger.error(f"错误回调执行失败: {e}")

    def set_camera(self, camera_controller) -> None:
        """
        设置相机控制器

        Args:
            camera_controller: 相机控制器实例
        """
        self._camera = camera_controller

    def start_continuous(self, fps: int, callback: Callable[[np.ndarray, int], None],
                         mode: AcquisitionMode = AcquisitionMode.RECORDING,
                         duration: int = 0,
                         resolution_index: int = 0) -> Tuple[bool, Optional[int]]:
        """
        启动连续采集

        Args:
            fps: 目标帧率（1-30）
            callback: 帧回调函数，参数为(图像数组, 帧序号)
            mode: 采集模式
            duration: 录像时长（秒），0表示手动停止
            resolution_index: 分辨率索引（0=1920x1080, 1=1280x720, 2=640x480）

        Returns:
            Tuple[bool, Optional[int]]: (是否启动成功, 错误码或None)
        """
        with self._lock:
            if self._running:
                logger.warning("采集已在运行中")
                return False, ErrorCode.STATE_RECORDING

            if not PYPYLON_AVAILABLE:
                logger.error("pypylon未安装，无法启动采集")
                return False, ErrorCode.CAMERA_NOT_CONNECTED

            if self._camera is None or not self._camera.is_connected:
                logger.error("相机未连接，无法启动采集")
                self._report_error(ErrorCode.CAMERA_NOT_CONNECTED, "相机未连接")
                return False, ErrorCode.CAMERA_NOT_CONNECTED

            #验证帧率范围
            fps = max(1, min(30, fps))

            #获取分辨率
            resolution = self.RESOLUTION_MAP.get(resolution_index, (1920, 1080))

            #配置采集参数
            self._config = AcquisitionConfig(
                fps=fps,
                resolution=resolution,
                duration=duration,
                buffer_size=fps * 2  #缓冲2秒的帧
            )

            #设置回调
            self._frame_callback = callback
            self._mode = mode

            #重置统计
            self._frame_count = 0
            self._start_time = time.time()
            self._actual_fps = 0.0
            self._timeout_count = 0
            self._last_error_code = None

            #清空队列
            while not self._frame_queue.empty():
                try:
                    self._frame_queue.get_nowait()
                except queue.Empty:
                    break

            #启动采集线程
            self._running = True
            self._acquisition_thread = threading.Thread(
                target=self._acquisition_loop,
                daemon=True,
                name=f"ImageAcquisition-{mode.name}"
            )
            self._acquisition_thread.start()

            logger.info(f"连续采集已启动: 模式={mode.name}, 帧率={fps}, 分辨率={resolution}, 时长={duration}秒")
            return True, None

    def stop_continuous(self) -> bool:
        """
        停止连续采集

        Returns:
            bool: 是否停止成功
        """
        with self._lock:
            if not self._running:
                logger.warning("采集未在运行")
                return False

            self._running = False

            #等待线程结束
            if self._acquisition_thread and self._acquisition_thread.is_alive():
                self._acquisition_thread.join(timeout=2.0)

            self._acquisition_thread = None
            self._mode = AcquisitionMode.IDLE

            #计算实际帧率
            elapsed = time.time() - self._start_time
            if elapsed > 0:
                self._actual_fps = self._frame_count / elapsed

            logger.info(f"连续采集已停止: 总帧数={self._frame_count}, 实际帧率={self._actual_fps:.2f}")
            return True

    def set_complete_callback(self, callback: Callable[[], None]) -> None:
        """
        设置采集完成回调

        Args:
            callback: 完成回调函数
        """
        self._complete_callback = callback

    def _acquisition_loop(self) -> None:
        """
        采集循环

        异常处理:
        - 采集超时: 记录警告，继续尝试，连续超时达到阈值则停止
        - 相机断线: 检测并报告错误
        - 其他异常: 记录错误并停止采集
        """
        frame_interval = 1.0 / self._config.fps
        next_frame_time = time.time()
        duration_end_time = 0

        #计算录像结束时间
        if self._config.duration > 0:
            duration_end_time = time.time() + self._config.duration

        logger.debug(f"采集循环开始: 帧间隔={frame_interval:.3f}秒")

        try:
            #获取pypylon相机对象
            if not hasattr(self._camera, '_camera') or self._camera._camera is None:
                logger.error("无法获取pypylon相机对象")
                self._report_error(ErrorCode.CAMERA_NOT_CONNECTED, "无法获取相机对象")
                return

            camera = self._camera._camera

            #开始连续采集
            try:
                camera.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)
            except pylon.RuntimeException as e:
                logger.error(f"启动采集失败: {e}")
                self._report_error(ErrorCode.CAMERA_DISCONNECTED, f"启动采集失败: {e}")
                return

            while self._running:
                #检查是否到达录像时长
                if duration_end_time > 0 and time.time() >= duration_end_time:
                    logger.info("录像时长已到，自动停止")
                    break

                #帧率控制
                current_time = time.time()
                if current_time < next_frame_time:
                    time.sleep(next_frame_time - current_time)

                next_frame_time = time.time() + frame_interval

                #采集图像
                try:
                    grab_result = camera.RetrieveResult(
                        5000,  #超时5秒
                        pylon.TimeoutHandling_ThrowException
                    )

                    if grab_result.GrabSucceeded():
                        #重置超时计数
                        self._timeout_count = 0

                        #获取图像数据
                        image = grab_result.Array.copy()
                        self._frame_count += 1

                        #调用回调
                        if self._frame_callback:
                            try:
                                self._frame_callback(image, self._frame_count)
                            except Exception as e:
                                logger.error(f"帧回调执行失败: {e}")

                        #更新实际帧率
                        elapsed = time.time() - self._start_time
                        if elapsed > 0:
                            self._actual_fps = self._frame_count / elapsed
                    else:
                        error_desc = grab_result.ErrorDescription if hasattr(grab_result, 'ErrorDescription') else "未知错误"
                        logger.warning(f"采集失败: {error_desc}")

                    grab_result.Release()

                except pylon.TimeoutException:
                    self._timeout_count += 1
                    logger.warning(f"采集超时 (第{self._timeout_count}次)")

                    #检查是否达到最大超时次数
                    if self._timeout_count >= self.MAX_TIMEOUT_COUNT:
                        logger.error(f"连续超时达到{self.MAX_TIMEOUT_COUNT}次，停止采集")
                        self._report_error(ErrorCode.CAMERA_GRAB_TIMEOUT, "连续采集超时")
                        break
                    continue

                except pylon.RuntimeException as e:
                    logger.error(f"采集异常（pypylon）: {e}")
                    #检测是否断线
                    if self._camera and hasattr(self._camera, '_check_connection'):
                        if not self._camera._check_connection():
                            self._report_error(ErrorCode.CAMERA_DISCONNECTED, "相机断线")
                            break
                    self._report_error(ErrorCode.CAMERA_GRAB_TIMEOUT, f"采集异常: {e}")
                    break

                except Exception as e:
                    logger.error(f"采集异常: {e}")
                    self._report_error(ErrorCode.CAMERA_GRAB_TIMEOUT, f"采集异常: {e}")
                    break

        except Exception as e:
            logger.error(f"采集循环异常: {e}")
            self._report_error(ErrorCode.CAMERA_GRAB_TIMEOUT, f"采集循环异常: {e}")
        finally:
            #停止采集
            try:
                if hasattr(self._camera, '_camera') and self._camera._camera:
                    if self._camera._camera.IsGrabbing():
                        self._camera._camera.StopGrabbing()
            except Exception as e:
                logger.debug(f"停止采集异常: {e}")

            #调用完成回调
            if self._complete_callback:
                try:
                    self._complete_callback()
                except Exception as e:
                    logger.error(f"完成回调执行失败: {e}")

            self._running = False
            self._mode = AcquisitionMode.IDLE
            logger.debug("采集循环结束")

    def get_frame(self, timeout: float = 1.0) -> Optional[Tuple[np.ndarray, int]]:
        """
        从缓冲队列获取帧

        Args:
            timeout: 超时时间（秒）

        Returns:
            Optional[Tuple[np.ndarray, int]]: (图像数组, 帧序号)，超时返回None
        """
        try:
            return self._frame_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def get_statistics(self) -> dict:
        """
        获取采集统计信息

        Returns:
            dict: 统计信息
        """
        elapsed = time.time() - self._start_time if self._start_time > 0 else 0
        return {
            "mode": self._mode.name,
            "running": self._running,
            "frame_count": self._frame_count,
            "elapsed_time": elapsed,
            "actual_fps": self._actual_fps,
            "target_fps": self._config.fps,
            "resolution": self._config.resolution,
        }


#========== 预览采集器 ==========

@dataclass
class PreviewConfig:
    """预览配置"""
    width: int = 1920           #预览宽度
    height: int = 1080          #预览高度
    fps: int = 10               #帧率
    jpeg_quality: int = 80      #JPEG质量
    #性能优化参数
    enable_skip_frame: bool = True      #启用跳帧策略
    enable_dynamic_quality: bool = True #启用动态质量调整
    min_quality: int = 30               #最低JPEG质量
    max_quality: int = 90               #最高JPEG质量
    buffer_pool_size: int = 10          #缓冲池大小


#预览分辨率映射表
PREVIEW_RESOLUTIONS = {
    0: (1920, 1080),    #索引0: 1920x1080
    1: (1280, 720),     #索引1: 1280x720
    2: (640, 480),      #索引2: 640x480
}


class PreviewAcquisition:
    """
    预览采集器

    专门用于实时预览功能，支持：
    - 帧率控制（5-30fps）
    - 分辨率缩放
    - JPEG编码
    - 异步帧推送

    说明:
        相机已配置为BGR8直出，无需软件格式转换
    """

    def __init__(self, camera_controller=None):
        """
        初始化预览采集器

        Args:
            camera_controller: 相机控制器实例
        """
        self._camera = camera_controller
        self._config = PreviewConfig()

        #预览线程
        self._preview_thread: Optional[threading.Thread] = None
        self._running = False
        self._is_previewing = False

        #帧序号计数器
        self._frame_seq = 0

        #帧回调函数（用于发送预览帧）
        self._on_preview_frame: Optional[Callable[[int, bytes], None]] = None

        #统计信息
        self._start_time = 0.0
        self._actual_fps = 0.0

        #线程锁
        self._lock = threading.RLock()

        #========== 性能优化组件 ==========
        #拥塞检测器
        self._congestion_detector: Optional['CongestionDetector'] = None
        if PERFORMANCE_UTILS_AVAILABLE:
            self._congestion_detector = CongestionDetector(
                latency_threshold_ms=100.0,
                queue_threshold=5,
                history_size=30
            )

        #性能监控器
        self._perf_monitor: Optional['PerformanceMonitor'] = None
        if PERFORMANCE_UTILS_AVAILABLE:
            self._perf_monitor = PerformanceMonitor(window_size=60)

        #图像缓冲池（延迟初始化）
        self._buffer_pool: Optional['ImageBufferPool'] = None

        #跳帧计数
        self._skipped_frames = 0

        #当前动态质量
        self._current_quality = 80

        logger.info("预览采集器初始化完成")

    @property
    def is_previewing(self) -> bool:
        """是否正在预览"""
        return self._is_previewing

    @property
    def frame_seq(self) -> int:
        """获取当前帧序号"""
        return self._frame_seq

    @property
    def actual_fps(self) -> float:
        """获取实际帧率"""
        return self._actual_fps

    def set_camera(self, camera_controller) -> None:
        """
        设置相机控制器

        Args:
            camera_controller: 相机控制器实例
        """
        self._camera = camera_controller

    def set_preview_callback(self, callback: Callable[[int, bytes], None]) -> None:
        """
        设置预览帧回调函数

        Args:
            callback: 回调函数，参数为(帧序号, JPEG数据)
        """
        self._on_preview_frame = callback

    def start_preview(self, resolution_index: int = 0, fps: int = 10) -> Tuple[bool, Optional[int]]:
        """
        开启实时预览

        Args:
            resolution_index: 分辨率索引（0=1920x1080, 1=1280x720, 2=640x480）
            fps: 帧率（5-30）

        Returns:
            Tuple[bool, Optional[int]]: (是否成功, 错误码或None)
        """
        with self._lock:
            #检查状态
            if self._running:
                logger.warning("预览已在运行中")
                return False, 0x0301  #状态冲突

            #检查相机连接
            if self._camera is None or not self._camera.is_connected:
                logger.error("相机未连接，无法开启预览")
                return False, 0x0101  #相机未连接

            #验证参数
            if resolution_index not in PREVIEW_RESOLUTIONS:
                logger.warning(f"无效的分辨率索引: {resolution_index}，使用默认值0")
                resolution_index = 0

            fps = max(5, min(30, fps))  #限制帧率范围

            #获取分辨率
            width, height = PREVIEW_RESOLUTIONS[resolution_index]

            #更新配置
            self._config.width = width
            self._config.height = height
            self._config.fps = fps

            #重置统计
            self._frame_seq = 0
            self._start_time = time.time()
            self._actual_fps = 0.0
            self._skipped_frames = 0
            self._current_quality = self._config.jpeg_quality

            #初始化缓冲池（如果启用）
            if PERFORMANCE_UTILS_AVAILABLE and self._config.buffer_pool_size > 0:
                #根据分辨率确定缓冲区形状（假设BGR格式）
                buffer_shape = (height, width, 3)
                self._buffer_pool = ImageBufferPool(
                    pool_size=self._config.buffer_pool_size,
                    buffer_shape=buffer_shape
                )
                logger.info(f"缓冲池已初始化: {self._config.buffer_pool_size}个缓冲区")

            #重置性能监控器
            if self._perf_monitor:
                self._perf_monitor.reset()

            #重置拥塞检测器
            if self._congestion_detector:
                self._congestion_detector.reset()

            #启动预览线程
            self._running = True
            self._is_previewing = True
            self._preview_thread = threading.Thread(
                target=self._preview_loop,
                daemon=True,
                name="PreviewThread"
            )
            self._preview_thread.start()

            logger.info(f"预览已开启: {width}x{height} @ {fps}fps")
            return True, None

    def stop_preview(self) -> Tuple[bool, Optional[int]]:
        """
        停止实时预览

        Returns:
            Tuple[bool, Optional[int]]: (是否成功, 错误码或None)
        """
        with self._lock:
            if not self._running:
                logger.warning("预览未在运行")
                return True, None  #已经停止，视为成功

            self._running = False

        #等待线程结束（不在锁内等待，避免死锁）
        if self._preview_thread and self._preview_thread.is_alive():
            self._preview_thread.join(timeout=2.0)

        with self._lock:
            self._is_previewing = False
            self._preview_thread = None

            #计算最终帧率
            elapsed = time.time() - self._start_time
            if elapsed > 0:
                self._actual_fps = self._frame_seq / elapsed

            #清理缓冲池
            if self._buffer_pool:
                self._buffer_pool.clear()
                self._buffer_pool = None

        logger.info(f"预览已停止: 总帧数={self._frame_seq}, 跳帧数={self._skipped_frames}, 实际帧率={self._actual_fps:.2f}")
        return True, None

    def _preview_loop(self) -> None:
        """
        预览采集循环

        性能优化:
        - 使用GrabStrategy_LatestImageOnly避免帧堆积
        - 跳帧策略（网络拥塞时跳过帧）
        - 动态JPEG质量调整
        - 性能监控

        说明:
            相机已配置为BGR8直出，无需软件格式转换
        """
        frame_interval = 1.0 / self._config.fps
        target_width = self._config.width
        target_height = self._config.height

        logger.debug(f"预览循环启动: 帧间隔={frame_interval*1000:.1f}ms, 目标分辨率={target_width}x{target_height}")

        try:
            #获取pypylon相机对象
            if not hasattr(self._camera, '_camera') or self._camera._camera is None:
                logger.error("无法获取pypylon相机对象")
                return

            camera = self._camera._camera

            #开始连续采集（使用LatestImageOnly策略避免帧堆积）
            camera.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)

            while self._running:
                loop_start = time.perf_counter()

                try:
                    #采集图像
                    grab_result = camera.RetrieveResult(
                        5000,  #超时5秒
                        pylon.TimeoutHandling_ThrowException
                    )

                    if grab_result.GrabSucceeded():
                        #递增帧序号
                        self._frame_seq += 1

                        #========== 跳帧策略 ==========
                        should_skip = False
                        if self._config.enable_skip_frame and self._congestion_detector:
                            should_skip = self._congestion_detector.should_skip_frame(self._frame_seq)

                        if should_skip:
                            self._skipped_frames += 1
                            if self._perf_monitor:
                                self._perf_monitor.record_dropped_frame()
                            grab_result.Release()
                            continue

                        #========== 动态质量调整 ==========
                        if self._config.enable_dynamic_quality and self._congestion_detector:
                            state = self._congestion_detector.get_state()
                            self._current_quality = max(
                                self._config.min_quality,
                                min(self._config.max_quality, state.recommended_quality)
                            )

                        #BGR8直出，直接获取图像数据
                        image = grab_result.Array

                        #缩放图像
                        resize_start = time.perf_counter()
                        resized = self._resize_image_optimized(image, target_width, target_height)
                        resize_time = (time.perf_counter() - resize_start) * 1000

                        #JPEG编码（使用动态质量）
                        encode_start = time.perf_counter()
                        jpeg_data = self._encode_jpeg(resized, self._current_quality)
                        encode_time = (time.perf_counter() - encode_start) * 1000

                        if self._perf_monitor:
                            self._perf_monitor.record_encode_time(encode_time)

                        if jpeg_data:
                            #更新实际帧率
                            elapsed = time.time() - self._start_time
                            if elapsed > 0:
                                self._actual_fps = self._frame_seq / elapsed

                            #调用回调发送帧
                            if self._on_preview_frame:
                                send_start = time.perf_counter()
                                try:
                                    self._on_preview_frame(self._frame_seq, jpeg_data)
                                except Exception as e:
                                    logger.error(f"预览帧回调执行失败: {e}")
                                send_time = (time.perf_counter() - send_start) * 1000

                                if self._perf_monitor:
                                    self._perf_monitor.record_send_time(send_time)

                    grab_result.Release()

                except pylon.TimeoutException:
                    logger.warning("预览采集超时")
                    continue
                except Exception as e:
                    logger.error(f"预览采集异常: {e}")
                    if not self._running:
                        break
                    continue

                #记录帧处理时间
                frame_time = (time.perf_counter() - loop_start) * 1000
                if self._perf_monitor:
                    self._perf_monitor.record_frame_time(frame_time)

                #帧率控制
                elapsed = time.perf_counter() - loop_start
                sleep_time = frame_interval - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)

        except Exception as e:
            logger.error(f"预览循环异常: {e}")
        finally:
            #停止采集
            try:
                if hasattr(self._camera, '_camera') and self._camera._camera:
                    if self._camera._camera.IsGrabbing():
                        self._camera._camera.StopGrabbing()
            except Exception as e:
                logger.debug(f"停止预览采集异常: {e}")

            self._running = False
            self._is_previewing = False
            logger.debug("预览循环结束")

    def _resize_image_optimized(self, image: np.ndarray, target_width: int, target_height: int) -> np.ndarray:
        """
        优化的图像缩放

        优先使用快速最近邻缩放，回退到Pillow

        Args:
            image: 原始图像
            target_width: 目标宽度
            target_height: 目标高度

        Returns:
            np.ndarray: 缩放后的图像
        """
        h, w = image.shape[:2]

        if w == target_width and h == target_height:
            return image

        #尝试使用快速缩放
        if PERFORMANCE_UTILS_AVAILABLE:
            try:
                return fast_resize_nearest(image, target_width, target_height)
            except Exception as e:
                logger.debug(f"快速缩放失败，回退到Pillow: {e}")

        #回退到原有的Pillow缩放
        return self._resize_image(image, target_width, target_height)

    def _resize_image(self, image: np.ndarray, target_width: int, target_height: int) -> np.ndarray:
        """
        缩放图像到目标尺寸

        Args:
            image: 原始图像（numpy数组）
            target_width: 目标宽度
            target_height: 目标高度

        Returns:
            np.ndarray: 缩放后的图像
        """
        try:
            #检查是否需要缩放
            h, w = image.shape[:2]

            if w == target_width and h == target_height:
                return image

            if not PIL_AVAILABLE:
                #Pillow不可用，返回原图
                logger.warning("Pillow不可用，无法缩放图像")
                return image

            #使用Pillow进行缩放
            if len(image.shape) == 3:
                #彩色图像，BGR转RGB
                rgb_image = image[:, :, ::-1]
                pil_image = Image.fromarray(rgb_image)
            else:
                #灰度图像
                pil_image = Image.fromarray(image)

            #缩放
            resized_pil = pil_image.resize((target_width, target_height), Image.BILINEAR)

            #转回numpy数组
            resized = np.array(resized_pil)

            #如果是彩色图像，RGB转回BGR
            if len(resized.shape) == 3:
                resized = resized[:, :, ::-1]

            return resized

        except Exception as e:
            logger.error(f"图像缩放失败: {e}")
            return image  #返回原图

    def _encode_jpeg(self, image: np.ndarray, quality: int = 80) -> Optional[bytes]:
        """
        将numpy数组编码为JPEG字节流

        Args:
            image: 图像数组
            quality: JPEG质量（1-100）

        Returns:
            Optional[bytes]: JPEG字节数据，失败返回None
        """
        try:
            if not PIL_AVAILABLE:
                logger.error("Pillow不可用，无法编码JPEG")
                return None

            #BGR转RGB（如果是彩色图像）
            if len(image.shape) == 3:
                rgb_image = image[:, :, ::-1]
            else:
                rgb_image = image

            #创建PIL图像
            pil_image = Image.fromarray(rgb_image)

            #编码为JPEG
            buffer = io.BytesIO()
            pil_image.save(buffer, format='JPEG', quality=quality)
            return buffer.getvalue()

        except Exception as e:
            logger.error(f"JPEG编码失败: {e}")
            return None

    def get_preview_info(self) -> dict:
        """
        获取预览信息

        Returns:
            dict: 预览信息字典
        """
        info = {
            "is_previewing": self._is_previewing,
            "width": self._config.width,
            "height": self._config.height,
            "fps": self._config.fps,
            "frame_seq": self._frame_seq,
            "actual_fps": self._actual_fps,
            "skipped_frames": self._skipped_frames,
            "current_quality": self._current_quality,
        }

        #添加性能监控信息
        if self._perf_monitor:
            metrics = self._perf_monitor.get_metrics()
            info["perf_metrics"] = {
                "fps": metrics.fps,
                "frame_time_ms": metrics.frame_time_ms,
                "encode_time_ms": metrics.encode_time_ms,
                "send_time_ms": metrics.send_time_ms,
                "dropped_frames": metrics.dropped_frames,
                "total_frames": metrics.total_frames,
            }

        #添加拥塞状态
        if self._congestion_detector:
            state = self._congestion_detector.get_state()
            info["congestion"] = {
                "is_congested": state.is_congested,
                "level": state.congestion_level,
                "recommended_quality": state.recommended_quality,
                "recommended_skip": state.recommended_skip,
            }

        #添加缓冲池状态
        if self._buffer_pool:
            pool_stats = self._buffer_pool.get_statistics()
            info["buffer_pool"] = {
                "available": pool_stats["available"],
                "in_use": pool_stats["in_use"],
                "miss_count": pool_stats["miss_count"],
            }

        return info

    def update_congestion_state(self, queue_size: int) -> None:
        """
        更新拥塞状态（由外部调用）

        Args:
            queue_size: 发送队列大小
        """
        if self._congestion_detector:
            self._congestion_detector.update_queue_size(queue_size)

    def set_performance_config(self,
                               enable_skip_frame: bool = None,
                               enable_dynamic_quality: bool = None,
                               min_quality: int = None,
                               max_quality: int = None) -> None:
        """
        设置性能优化配置

        Args:
            enable_skip_frame: 是否启用跳帧
            enable_dynamic_quality: 是否启用动态质量
            min_quality: 最低JPEG质量
            max_quality: 最高JPEG质量
        """
        with self._lock:
            if enable_skip_frame is not None:
                self._config.enable_skip_frame = enable_skip_frame
            if enable_dynamic_quality is not None:
                self._config.enable_dynamic_quality = enable_dynamic_quality
            if min_quality is not None:
                self._config.min_quality = max(1, min(100, min_quality))
            if max_quality is not None:
                self._config.max_quality = max(1, min(100, max_quality))

        logger.info(f"性能配置已更新: skip_frame={self._config.enable_skip_frame}, "
                   f"dynamic_quality={self._config.enable_dynamic_quality}, "
                   f"quality_range=[{self._config.min_quality}, {self._config.max_quality}]")
