# -*- coding: utf-8 -*-
"""
相机控制模块
基于pypylon SDK实现Basler相机的连接、参数设置和图像采集
支持相机型号: Basler acA5472-gc

异常处理:
- 相机断线检测和自动重连
- 采集超时处理
- 参数设置失败处理
- 初始化失败处理
"""

import threading
import time
from enum import Enum
from typing import Optional, List, Tuple, Dict, Any, Callable
from dataclasses import dataclass

import numpy as np
from loguru import logger

try:
    from pypylon import pylon
    PYPYLON_AVAILABLE = True
except ImportError:
    PYPYLON_AVAILABLE = False
    logger.warning("pypylon未安装，相机功能不可用")

#导入错误码
try:
    from .utils.errors import ErrorCode
except ImportError:
    #独立运行时的兼容处理
    class ErrorCode:
        CAMERA_NOT_CONNECTED = 0x0101
        CAMERA_INIT_FAILED = 0x0102
        CAMERA_GRAB_TIMEOUT = 0x0103
        CAMERA_PARAM_FAILED = 0x0104
        CAMERA_DISCONNECTED = 0x0105
        CAMERA_UNSUPPORTED_RES = 0x0106


class CameraState(Enum):
    """相机状态枚举"""
    DISCONNECTED = 0  #未连接
    CONNECTED = 1     #已连接
    GRABBING = 2      #采集中
    ERROR = 3         #错误状态


class ExposureMode(Enum):
    """曝光模式枚举"""
    AUTO = "Continuous"   #自动曝光
    MANUAL = "Off"        #手动曝光


class WhiteBalanceMode(Enum):
    """白平衡模式枚举"""
    AUTO = "Continuous"   #自动白平衡
    MANUAL = "Off"        #手动白平衡


@dataclass
class CameraInfo:
    """相机信息数据类"""
    serial_number: str
    model_name: str
    vendor_name: str
    device_class: str
    friendly_name: str


@dataclass
class CameraParameters:
    """相机参数数据类"""
    exposure_time: float      #曝光时间（微秒）
    gain: float               #增益
    width: int                #图像宽度
    height: int               #图像高度
    offset_x: int             #X偏移
    offset_y: int             #Y偏移
    exposure_mode: str        #曝光模式
    white_balance_mode: str   #白平衡模式


class CameraController:
    """
    相机控制器
    提供相机连接、参数设置、图像采集等功能

    异常处理机制:
    - 连接失败: 返回False并设置ERROR状态
    - 断线检测: 通过_check_connection()检测，触发自动重连
    - 采集超时: 捕获TimeoutException，返回None
    - 参数设置失败: 捕获异常，返回False
    """

    #最大重连次数
    MAX_RECONNECT_ATTEMPTS = 10

    def __init__(self, config_manager=None):
        """
        初始化相机控制器

        Args:
            config_manager: 配置管理器实例

        Raises:
            RuntimeError: pypylon SDK未安装时抛出
        """
        if not PYPYLON_AVAILABLE:
            raise RuntimeError("pypylon SDK未安装，无法使用相机功能")

        self._camera: Optional[pylon.InstantCamera] = None
        self._tl_factory = pylon.TlFactory.GetInstance()
        self._state = CameraState.DISCONNECTED
        self._config = config_manager
        self._last_error_code: Optional[int] = None  #最后一次错误码

        #重连相关
        self._reconnect_thread: Optional[threading.Thread] = None
        self._reconnect_running = False
        self._reconnect_interval = 5  #默认重连间隔（秒）
        self._reconnect_attempts = 0  #当前重连尝试次数
        if config_manager:
            self._reconnect_interval = config_manager.camera_reconnect_interval

        #回调函数
        self._on_disconnected: Optional[Callable] = None
        self._on_reconnected: Optional[Callable] = None
        self._on_error: Optional[Callable[[int, str], None]] = None  #错误回调(错误码, 描述)

        #线程锁
        self._lock = threading.RLock()

        #采集超时（毫秒）
        self._grab_timeout = 5000
        if config_manager:
            self._grab_timeout = config_manager.camera_grab_timeout

        #图像格式转换器（当相机不支持BGR8时启用）
        self._converter: Optional[pylon.ImageFormatConverter] = None
        self._use_converter = False

        logger.info("相机控制器初始化完成")

    @property
    def state(self) -> CameraState:
        """获取相机状态"""
        return self._state

    @property
    def is_connected(self) -> bool:
        """相机是否已连接"""
        return self._state in (CameraState.CONNECTED, CameraState.GRABBING)

    @property
    def last_error_code(self) -> Optional[int]:
        """获取最后一次错误码"""
        return self._last_error_code

    def set_error_callback(self, callback: Optional[Callable[[int, str], None]]) -> None:
        """
        设置错误回调函数

        Args:
            callback: 回调函数，参数为(错误码, 错误描述)
        """
        self._on_error = callback

    def _report_error(self, error_code: int, description: str) -> None:
        """
        报告错误

        Args:
            error_code: 错误码
            description: 错误描述
        """
        self._last_error_code = error_code
        logger.error(f"相机错误[0x{error_code:04X}]: {description}")
        if self._on_error:
            try:
                self._on_error(error_code, description)
            except Exception as e:
                logger.error(f"错误回调执行失败: {e}")

    def enumerate_cameras(self) -> List[CameraInfo]:
        """
        枚举所有可用的Basler相机

        Returns:
            相机信息列表，枚举失败返回空列表
        """
        cameras = []
        try:
            devices = self._tl_factory.EnumerateDevices()
            for device in devices:
                info = CameraInfo(
                    serial_number=device.GetSerialNumber(),
                    model_name=device.GetModelName(),
                    vendor_name=device.GetVendorName(),
                    device_class=device.GetDeviceClass(),
                    friendly_name=device.GetFriendlyName()
                )
                cameras.append(info)
                logger.debug(f"发现相机: {info.model_name} (SN: {info.serial_number})")

            logger.info(f"枚举到 {len(cameras)} 台相机")
        except pylon.RuntimeException as e:
            logger.error(f"枚举相机失败（pypylon异常）: {e}")
            self._report_error(ErrorCode.CAMERA_INIT_FAILED, f"枚举相机失败: {e}")
        except Exception as e:
            logger.error(f"枚举相机失败: {e}")
            self._report_error(ErrorCode.CAMERA_INIT_FAILED, f"枚举相机失败: {e}")

        return cameras

    def connect(self, serial_number: Optional[str] = None) -> Tuple[bool, Optional[int]]:
        """
        连接相机

        Args:
            serial_number: 相机序列号，为None时连接第一个可用相机

        Returns:
            Tuple[bool, Optional[int]]: (是否连接成功, 错误码或None)
        """
        with self._lock:
            if self.is_connected:
                logger.warning("相机已连接，请先断开")
                return True, None

            try:
                if serial_number:
                    #通过序列号连接指定相机
                    devices = self._tl_factory.EnumerateDevices()
                    target_device = None
                    for device in devices:
                        if device.GetSerialNumber() == serial_number:
                            target_device = device
                            break

                    if target_device is None:
                        error_msg = f"未找到序列号为 {serial_number} 的相机"
                        logger.error(error_msg)
                        self._report_error(ErrorCode.CAMERA_NOT_CONNECTED, error_msg)
                        return False, ErrorCode.CAMERA_NOT_CONNECTED

                    self._camera = pylon.InstantCamera(
                        self._tl_factory.CreateDevice(target_device)
                    )
                else:
                    #连接第一个可用相机
                    try:
                        self._camera = pylon.InstantCamera(
                            self._tl_factory.CreateFirstDevice()
                        )
                    except pylon.RuntimeException as e:
                        error_msg = f"未找到可用相机: {e}"
                        logger.error(error_msg)
                        self._report_error(ErrorCode.CAMERA_NOT_CONNECTED, error_msg)
                        return False, ErrorCode.CAMERA_NOT_CONNECTED

                #打开相机
                self._camera.Open()

                #注册事件处理
                self._register_event_handlers()

                #应用默认参数
                self._apply_default_parameters()

                self._state = CameraState.CONNECTED
                self._reconnect_attempts = 0  #重置重连计数
                logger.info(f"相机连接成功: {self._camera.GetDeviceInfo().GetModelName()}")
                return True, None

            except pylon.RuntimeException as e:
                error_msg = f"连接相机失败（pypylon异常）: {e}"
                logger.error(error_msg)
                self._state = CameraState.ERROR
                self._report_error(ErrorCode.CAMERA_INIT_FAILED, error_msg)
                return False, ErrorCode.CAMERA_INIT_FAILED
            except Exception as e:
                error_msg = f"连接相机失败: {e}"
                logger.error(error_msg)
                self._state = CameraState.ERROR
                self._report_error(ErrorCode.CAMERA_INIT_FAILED, error_msg)
                return False, ErrorCode.CAMERA_INIT_FAILED

    def disconnect(self) -> bool:
        """
        断开相机连接

        Returns:
            是否断开成功
        """
        with self._lock:
            #停止重连线程
            self._stop_reconnect_thread()

            if self._camera is None:
                self._state = CameraState.DISCONNECTED
                return True

            try:
                if self._camera.IsGrabbing():
                    self._camera.StopGrabbing()

                if self._camera.IsOpen():
                    self._camera.Close()

                self._camera = None
                self._state = CameraState.DISCONNECTED
                logger.info("相机已断开连接")
                return True

            except Exception as e:
                logger.error(f"断开相机连接失败: {e}")
                self._camera = None
                self._state = CameraState.DISCONNECTED
                return False

    def _register_event_handlers(self) -> None:
        """注册相机事件处理器"""
        #pypylon的事件处理通过配置处理器实现
        #这里主要用于断线检测
        pass

    def _apply_default_parameters(self) -> None:
        """
        应用默认参数

        设置BGR8像素格式以直接输出彩色图像，避免软件转换
        如果相机不支持BGR8，回退到BayerRG8并记录警告
        """
        if self._camera is None:
            return

        #设置像素格式为BGR8（直接输出彩色），否则启用软件转换
        try:
            if hasattr(self._camera, 'PixelFormat'):
                #检查是否支持BGR8
                available_formats = self._camera.PixelFormat.Symbolics
                if 'BGR8' in available_formats:
                    self._camera.PixelFormat.SetValue('BGR8')
                    logger.info("像素格式设置为BGR8（直接彩色输出）")
                    self._use_converter = False
                elif 'BGR8Packed' in available_formats:
                    self._camera.PixelFormat.SetValue('BGR8Packed')
                    logger.info("像素格式设置为BGR8Packed（直接彩色输出）")
                    self._use_converter = False
                else:
                    #回退到BayerRG8，需要软件转换
                    if 'BayerRG8' in available_formats:
                        self._camera.PixelFormat.SetValue('BayerRG8')
                        logger.warning("相机不支持BGR8格式，已回退到BayerRG8（需软件转换）")
                    else:
                        logger.warning("相机不支持BGR8格式，使用默认像素格式（需软件转换）")
                    self._use_converter = True
                    self._ensure_converter()
        except Exception as e:
            logger.warning(f"设置像素格式失败: {e}")

        #应用配置文件中的默认参数
        if self._config:
            try:
                self.set_exposure(self._config.camera_default_exposure)
                self.set_gain(self._config.camera_default_gain)
            except Exception as e:
                logger.warning(f"应用默认参数失败: {e}")

    def _check_connection(self) -> bool:
        """
        检查相机连接状态

        Returns:
            相机是否正常连接
        """
        if self._camera is None:
            return False

        try:
            #尝试读取一个参数来检测连接
            _ = self._camera.IsOpen()
            return True
        except Exception:
            return False

    def _start_reconnect_thread(self) -> None:
        """启动自动重连线程"""
        if self._reconnect_running:
            return

        self._reconnect_running = True
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop,
            daemon=True,
            name="CameraReconnect"
        )
        self._reconnect_thread.start()
        logger.info("自动重连线程已启动")

    def _stop_reconnect_thread(self) -> None:
        """停止自动重连线程"""
        self._reconnect_running = False
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            self._reconnect_thread.join(timeout=2)
        self._reconnect_thread = None

    def _reconnect_loop(self) -> None:
        """
        重连循环

        实现带最大重试次数的自动重连机制
        """
        while self._reconnect_running:
            time.sleep(self._reconnect_interval)

            if not self._reconnect_running:
                break

            if self._state == CameraState.DISCONNECTED or self._state == CameraState.ERROR:
                #检查重连次数
                if self._reconnect_attempts >= self.MAX_RECONNECT_ATTEMPTS:
                    logger.error(f"相机重连失败，已达到最大重试次数({self.MAX_RECONNECT_ATTEMPTS})")
                    self._report_error(
                        ErrorCode.CAMERA_DISCONNECTED,
                        f"重连失败，已尝试{self._reconnect_attempts}次"
                    )
                    self._reconnect_running = False
                    break

                self._reconnect_attempts += 1
                logger.info(f"尝试重新连接相机... (第{self._reconnect_attempts}次)")

                serial = None
                if self._config:
                    serial = self._config.camera_serial or None

                success, _ = self.connect(serial)
                if success:
                    logger.info("相机重连成功")
                    self._reconnect_attempts = 0
                    if self._on_reconnected:
                        try:
                            self._on_reconnected()
                        except Exception as e:
                            logger.error(f"重连回调执行失败: {e}")
                    break
                else:
                    logger.warning(f"重连失败，{self._reconnect_interval}秒后重试")

    def enable_auto_reconnect(self, enabled: bool = True) -> None:
        """
        启用/禁用自动重连

        Args:
            enabled: 是否启用
        """
        if enabled:
            self._start_reconnect_thread()
        else:
            self._stop_reconnect_thread()

    def set_disconnect_callback(self, callback: Optional[Callable]) -> None:
        """设置断线回调函数"""
        self._on_disconnected = callback

    def set_reconnect_callback(self, callback: Optional[Callable]) -> None:
        """设置重连成功回调函数"""
        self._on_reconnected = callback

    #========== 参数设置 ==========

    def set_exposure(self, exposure_us: float, mode: ExposureMode = ExposureMode.MANUAL) -> Tuple[bool, Optional[int]]:
        """
        设置曝光时间

        Args:
            exposure_us: 曝光时间（微秒）
            mode: 曝光模式

        Returns:
            Tuple[bool, Optional[int]]: (是否设置成功, 错误码或None)
        """
        with self._lock:
            if not self.is_connected or self._camera is None:
                error_msg = "相机未连接，无法设置曝光"
                logger.error(error_msg)
                self._report_error(ErrorCode.CAMERA_NOT_CONNECTED, error_msg)
                return False, ErrorCode.CAMERA_NOT_CONNECTED

            try:
                #设置曝光模式
                if hasattr(self._camera, 'ExposureAuto'):
                    self._camera.ExposureAuto.SetValue(mode.value)

                #手动模式下设置曝光时间
                if mode == ExposureMode.MANUAL:
                    #获取曝光范围
                    min_exp = self._camera.ExposureTime.Min
                    max_exp = self._camera.ExposureTime.Max

                    #范围检查
                    if exposure_us < min_exp or exposure_us > max_exp:
                        logger.warning(f"曝光值{exposure_us}超出范围[{min_exp}, {max_exp}]，自动调整")
                    exposure_us = max(min_exp, min(max_exp, exposure_us))
                    self._camera.ExposureTime.SetValue(exposure_us)
                    logger.info(f"曝光时间设置为: {exposure_us} us")

                return True, None

            except pylon.RuntimeException as e:
                error_msg = f"设置曝光失败（pypylon异常）: {e}"
                logger.error(error_msg)
                self._report_error(ErrorCode.CAMERA_PARAM_FAILED, error_msg)
                #检测是否断线
                if not self._check_connection():
                    self._handle_disconnection()
                return False, ErrorCode.CAMERA_PARAM_FAILED
            except Exception as e:
                error_msg = f"设置曝光失败: {e}"
                logger.error(error_msg)
                self._report_error(ErrorCode.CAMERA_PARAM_FAILED, error_msg)
                return False, ErrorCode.CAMERA_PARAM_FAILED

    def set_exposure_auto(self, enabled: bool = True) -> bool:
        """
        设置自动曝光

        Args:
            enabled: 是否启用自动曝光

        Returns:
            是否设置成功
        """
        mode = ExposureMode.AUTO if enabled else ExposureMode.MANUAL
        success, _ = self.set_exposure(0, mode) if enabled else (True, None)
        return success

    def _handle_disconnection(self) -> None:
        """
        处理相机断线

        设置错误状态，触发断线回调，启动自动重连
        """
        logger.warning("检测到相机断线")
        self._state = CameraState.ERROR
        self._report_error(ErrorCode.CAMERA_DISCONNECTED, "相机连接已断开")

        if self._on_disconnected:
            try:
                self._on_disconnected()
            except Exception as e:
                logger.error(f"断线回调执行失败: {e}")

        #启动自动重连
        self._start_reconnect_thread()

    def set_gain(self, gain: float) -> Tuple[bool, Optional[int]]:
        """
        设置增益

        Args:
            gain: 增益值

        Returns:
            Tuple[bool, Optional[int]]: (是否设置成功, 错误码或None)
        """
        with self._lock:
            if not self.is_connected or self._camera is None:
                error_msg = "相机未连接，无法设置增益"
                logger.error(error_msg)
                self._report_error(ErrorCode.CAMERA_NOT_CONNECTED, error_msg)
                return False, ErrorCode.CAMERA_NOT_CONNECTED

            try:
                #关闭自动增益
                if hasattr(self._camera, 'GainAuto'):
                    self._camera.GainAuto.SetValue("Off")

                #获取增益范围
                min_gain = self._camera.Gain.Min
                max_gain = self._camera.Gain.Max

                #范围检查
                if gain < min_gain or gain > max_gain:
                    logger.warning(f"增益值{gain}超出范围[{min_gain}, {max_gain}]，自动调整")
                gain = max(min_gain, min(max_gain, gain))
                self._camera.Gain.SetValue(gain)
                logger.info(f"增益设置为: {gain}")
                return True, None

            except pylon.RuntimeException as e:
                error_msg = f"设置增益失败（pypylon异常）: {e}"
                logger.error(error_msg)
                self._report_error(ErrorCode.CAMERA_PARAM_FAILED, error_msg)
                #检测是否断线
                if not self._check_connection():
                    self._handle_disconnection()
                return False, ErrorCode.CAMERA_PARAM_FAILED
            except Exception as e:
                error_msg = f"设置增益失败: {e}"
                logger.error(error_msg)
                self._report_error(ErrorCode.CAMERA_PARAM_FAILED, error_msg)
                return False, ErrorCode.CAMERA_PARAM_FAILED

    def set_gain_auto(self, enabled: bool = True) -> Tuple[bool, Optional[int]]:
        """
        设置自动增益

        Args:
            enabled: 是否启用自动增益

        Returns:
            Tuple[bool, Optional[int]]: (是否设置成功, 错误码或None)
        """
        with self._lock:
            if not self.is_connected or self._camera is None:
                error_msg = "相机未连接，无法设置自动增益"
                logger.error(error_msg)
                self._report_error(ErrorCode.CAMERA_NOT_CONNECTED, error_msg)
                return False, ErrorCode.CAMERA_NOT_CONNECTED

            if not hasattr(self._camera, 'GainAuto'):
                error_msg = "相机不支持自动增益"
                logger.warning(error_msg)
                self._report_error(ErrorCode.CAMERA_PARAM_FAILED, error_msg)
                return False, ErrorCode.CAMERA_PARAM_FAILED

            try:
                mode = "Continuous" if enabled else "Off"
                self._camera.GainAuto.SetValue(mode)
                logger.info(f"自动增益设置为: {mode}")
                return True, None

            except pylon.RuntimeException as e:
                error_msg = f"设置自动增益失败（pypylon异常）: {e}"
                logger.error(error_msg)
                self._report_error(ErrorCode.CAMERA_PARAM_FAILED, error_msg)
                if not self._check_connection():
                    self._handle_disconnection()
                return False, ErrorCode.CAMERA_PARAM_FAILED
            except Exception as e:
                error_msg = f"设置自动增益失败: {e}"
                logger.error(error_msg)
                self._report_error(ErrorCode.CAMERA_PARAM_FAILED, error_msg)
                return False, ErrorCode.CAMERA_PARAM_FAILED

    def set_white_balance(self, mode: WhiteBalanceMode = WhiteBalanceMode.AUTO,
                          red_ratio: float = 1.0, green_ratio: float = 1.0,
                          blue_ratio: float = 1.0) -> Tuple[bool, Optional[int]]:
        """
        设置白平衡

        Args:
            mode: 白平衡模式
            red_ratio: 红色通道比例（手动模式）
            green_ratio: 绿色通道比例（手动模式）
            blue_ratio: 蓝色通道比例（手动模式）

        Returns:
            Tuple[bool, Optional[int]]: (是否设置成功, 错误码或None)
        """
        with self._lock:
            if not self.is_connected or self._camera is None:
                error_msg = "相机未连接，无法设置白平衡"
                logger.error(error_msg)
                self._report_error(ErrorCode.CAMERA_NOT_CONNECTED, error_msg)
                return False, ErrorCode.CAMERA_NOT_CONNECTED

            try:
                #检查是否支持白平衡
                if not hasattr(self._camera, 'BalanceWhiteAuto'):
                    logger.warning("相机不支持白平衡设置")
                    return False, ErrorCode.CAMERA_PARAM_FAILED

                #设置白平衡模式
                self._camera.BalanceWhiteAuto.SetValue(mode.value)

                #手动模式下设置比例
                if mode == WhiteBalanceMode.MANUAL:
                    if hasattr(self._camera, 'BalanceRatioSelector'):
                        #获取白平衡比例范围
                        min_ratio = self._camera.BalanceRatio.Min
                        max_ratio = self._camera.BalanceRatio.Max

                        #设置红色通道
                        self._camera.BalanceRatioSelector.SetValue("Red")
                        red_ratio = max(min_ratio, min(max_ratio, red_ratio))
                        self._camera.BalanceRatio.SetValue(red_ratio)

                        #设置绿色通道
                        self._camera.BalanceRatioSelector.SetValue("Green")
                        green_ratio = max(min_ratio, min(max_ratio, green_ratio))
                        self._camera.BalanceRatio.SetValue(green_ratio)

                        #设置蓝色通道
                        self._camera.BalanceRatioSelector.SetValue("Blue")
                        blue_ratio = max(min_ratio, min(max_ratio, blue_ratio))
                        self._camera.BalanceRatio.SetValue(blue_ratio)

                        logger.info(f"白平衡设置为: R={red_ratio:.2f}, G={green_ratio:.2f}, B={blue_ratio:.2f}")

                logger.info(f"白平衡模式设置为: {mode.name}")
                return True, None

            except pylon.RuntimeException as e:
                error_msg = f"设置白平衡失败（pypylon异常）: {e}"
                logger.error(error_msg)
                self._report_error(ErrorCode.CAMERA_PARAM_FAILED, error_msg)
                #检测是否断线
                if not self._check_connection():
                    self._handle_disconnection()
                return False, ErrorCode.CAMERA_PARAM_FAILED
            except Exception as e:
                error_msg = f"设置白平衡失败: {e}"
                logger.error(error_msg)
                self._report_error(ErrorCode.CAMERA_PARAM_FAILED, error_msg)
                return False, ErrorCode.CAMERA_PARAM_FAILED

    def set_resolution(self, width: int, height: int,
                       offset_x: int = 0, offset_y: int = 0) -> Tuple[bool, Optional[int]]:
        """
        设置分辨率

        Args:
            width: 图像宽度
            height: 图像高度
            offset_x: X偏移
            offset_y: Y偏移

        Returns:
            Tuple[bool, Optional[int]]: (是否设置成功, 错误码或None)
        """
        with self._lock:
            if not self.is_connected or self._camera is None:
                error_msg = "相机未连接，无法设置分辨率"
                logger.error(error_msg)
                self._report_error(ErrorCode.CAMERA_NOT_CONNECTED, error_msg)
                return False, ErrorCode.CAMERA_NOT_CONNECTED

            try:
                #获取最大分辨率
                max_width = self._camera.Width.Max
                max_height = self._camera.Height.Max

                #检查分辨率是否超出范围
                if width > max_width or height > max_height:
                    error_msg = f"分辨率{width}x{height}超出最大值{max_width}x{max_height}"
                    logger.error(error_msg)
                    self._report_error(ErrorCode.CAMERA_UNSUPPORTED_RES, error_msg)
                    return False, ErrorCode.CAMERA_UNSUPPORTED_RES

                #范围检查
                width = max(1, min(max_width, width))
                height = max(1, min(max_height, height))
                offset_x = max(0, min(max_width - width, offset_x))
                offset_y = max(0, min(max_height - height, offset_y))

                #设置偏移（需要先设置，否则可能超出范围）
                self._camera.OffsetX.SetValue(0)
                self._camera.OffsetY.SetValue(0)

                #设置分辨率
                self._camera.Width.SetValue(width)
                self._camera.Height.SetValue(height)

                #设置偏移
                self._camera.OffsetX.SetValue(offset_x)
                self._camera.OffsetY.SetValue(offset_y)

                logger.info(f"分辨率设置为: {width}x{height}, 偏移: ({offset_x}, {offset_y})")
                return True, None

            except pylon.RuntimeException as e:
                error_msg = f"设置分辨率失败（pypylon异常）: {e}"
                logger.error(error_msg)
                self._report_error(ErrorCode.CAMERA_PARAM_FAILED, error_msg)
                #检测是否断线
                if not self._check_connection():
                    self._handle_disconnection()
                return False, ErrorCode.CAMERA_PARAM_FAILED
            except Exception as e:
                error_msg = f"设置分辨率失败: {e}"
                logger.error(error_msg)
                self._report_error(ErrorCode.CAMERA_PARAM_FAILED, error_msg)
                return False, ErrorCode.CAMERA_PARAM_FAILED

    def set_frame_rate(self, fps: float, enable: bool = True) -> Tuple[bool, Optional[int]]:
        """
        设置采集帧率

        Args:
            fps: 帧率值
            enable: 是否启用帧率限制

        Returns:
            Tuple[bool, Optional[int]]: (是否设置成功, 错误码或None)
        """
        with self._lock:
            if not self.is_connected or self._camera is None:
                error_msg = "相机未连接，无法设置帧率"
                logger.error(error_msg)
                self._report_error(ErrorCode.CAMERA_NOT_CONNECTED, error_msg)
                return False, ErrorCode.CAMERA_NOT_CONNECTED

            if not hasattr(self._camera, 'AcquisitionFrameRate'):
                error_msg = "相机不支持帧率设置"
                logger.warning(error_msg)
                self._report_error(ErrorCode.CAMERA_PARAM_FAILED, error_msg)
                return False, ErrorCode.CAMERA_PARAM_FAILED

            try:
                if hasattr(self._camera, 'AcquisitionFrameRateEnable'):
                    self._camera.AcquisitionFrameRateEnable.SetValue(bool(enable))
                elif not enable:
                    logger.warning("相机不支持帧率开关，已忽略禁用请求")

                if enable:
                    min_fps = self._camera.AcquisitionFrameRate.Min
                    max_fps = self._camera.AcquisitionFrameRate.Max
                    if fps < min_fps or fps > max_fps:
                        logger.warning(f"帧率{fps}超出范围[{min_fps}, {max_fps}]，自动调整")
                    fps = max(min_fps, min(max_fps, fps))
                    self._camera.AcquisitionFrameRate.SetValue(fps)
                    logger.info(f"帧率设置为: {fps:.2f} fps")
                else:
                    logger.info("帧率限制已关闭")

                return True, None

            except pylon.RuntimeException as e:
                error_msg = f"设置帧率失败（pypylon异常）: {e}"
                logger.error(error_msg)
                self._report_error(ErrorCode.CAMERA_PARAM_FAILED, error_msg)
                if not self._check_connection():
                    self._handle_disconnection()
                return False, ErrorCode.CAMERA_PARAM_FAILED
            except Exception as e:
                error_msg = f"设置帧率失败: {e}"
                logger.error(error_msg)
                self._report_error(ErrorCode.CAMERA_PARAM_FAILED, error_msg)
                return False, ErrorCode.CAMERA_PARAM_FAILED

    def set_pixel_format(self, format_name: str) -> Tuple[bool, Optional[int]]:
        """
        设置像素格式

        Args:
            format_name: 像素格式名称（如BGR8、Mono8等）

        Returns:
            Tuple[bool, Optional[int]]: (是否设置成功, 错误码或None)
        """
        with self._lock:
            if not self.is_connected or self._camera is None:
                error_msg = "相机未连接，无法设置像素格式"
                logger.error(error_msg)
                self._report_error(ErrorCode.CAMERA_NOT_CONNECTED, error_msg)
                return False, ErrorCode.CAMERA_NOT_CONNECTED

            if not hasattr(self._camera, 'PixelFormat'):
                error_msg = "相机不支持像素格式设置"
                logger.warning(error_msg)
                self._report_error(ErrorCode.CAMERA_PARAM_FAILED, error_msg)
                return False, ErrorCode.CAMERA_PARAM_FAILED

            try:
                available_formats = self._camera.PixelFormat.Symbolics
                target_format = format_name
                if format_name == "BGR8" and "BGR8" not in available_formats and "BGR8Packed" in available_formats:
                    target_format = "BGR8Packed"

                if target_format not in available_formats:
                    error_msg = f"像素格式不支持: {format_name}"
                    logger.warning(error_msg)
                    self._report_error(ErrorCode.CAMERA_PARAM_FAILED, error_msg)
                    return False, ErrorCode.CAMERA_PARAM_FAILED

                self._camera.PixelFormat.SetValue(target_format)
                self._use_converter = target_format not in ("BGR8", "BGR8Packed")
                if self._use_converter:
                    self._ensure_converter()
                logger.info(f"像素格式设置为: {target_format}")
                return True, None

            except pylon.RuntimeException as e:
                error_msg = f"设置像素格式失败（pypylon异常）: {e}"
                logger.error(error_msg)
                self._report_error(ErrorCode.CAMERA_PARAM_FAILED, error_msg)
                if not self._check_connection():
                    self._handle_disconnection()
                return False, ErrorCode.CAMERA_PARAM_FAILED
            except Exception as e:
                error_msg = f"设置像素格式失败: {e}"
                logger.error(error_msg)
                self._report_error(ErrorCode.CAMERA_PARAM_FAILED, error_msg)
                return False, ErrorCode.CAMERA_PARAM_FAILED

    #========== 图像采集 ==========

    def grab_single(self) -> Tuple[Optional[np.ndarray], Optional[int]]:
        """
        单帧采集

        Returns:
            Tuple[Optional[np.ndarray], Optional[int]]: (图像数据, 错误码)
            成功时返回(BGR格式图像数组, None)，失败时返回(None, 错误码)

            说明:
                当相机不支持BGR8时自动进行软件格式转换
        """
        with self._lock:
            if not self.is_connected or self._camera is None:
                error_msg = "相机未连接，无法采集图像"
                logger.error(error_msg)
                self._report_error(ErrorCode.CAMERA_NOT_CONNECTED, error_msg)
                return None, ErrorCode.CAMERA_NOT_CONNECTED

            try:
                #单次采集
                self._camera.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)
                grab_result = self._camera.RetrieveResult(
                    self._grab_timeout,
                    pylon.TimeoutHandling_ThrowException
                )

                if grab_result.GrabSucceeded():
                    image = self._convert_grab_result(grab_result)
                    grab_result.Release()
                    self._camera.StopGrabbing()
                    logger.debug(f"采集成功，图像尺寸: {image.shape}")
                    return image, None
                else:
                    error_msg = f"采集失败: {grab_result.ErrorCode} - {grab_result.ErrorDescription}"
                    logger.error(error_msg)
                    self._report_error(ErrorCode.CAMERA_GRAB_TIMEOUT, error_msg)
                    grab_result.Release()
                    self._camera.StopGrabbing()
                    return None, ErrorCode.CAMERA_GRAB_TIMEOUT

            except pylon.TimeoutException:
                error_msg = "采集超时"
                logger.error(error_msg)
                self._report_error(ErrorCode.CAMERA_GRAB_TIMEOUT, error_msg)
                if self._camera.IsGrabbing():
                    self._camera.StopGrabbing()
                return None, ErrorCode.CAMERA_GRAB_TIMEOUT
            except pylon.RuntimeException as e:
                error_msg = f"采集异常（pypylon）: {e}"
                logger.error(error_msg)
                if self._camera and self._camera.IsGrabbing():
                    self._camera.StopGrabbing()
                #检测是否断线
                if not self._check_connection():
                    self._handle_disconnection()
                    return None, ErrorCode.CAMERA_DISCONNECTED
                self._report_error(ErrorCode.CAMERA_GRAB_TIMEOUT, error_msg)
                return None, ErrorCode.CAMERA_GRAB_TIMEOUT
            except Exception as e:
                error_msg = f"采集异常: {e}"
                logger.error(error_msg)
                if self._camera and self._camera.IsGrabbing():
                    self._camera.StopGrabbing()
                #检测是否断线
                if not self._check_connection():
                    self._handle_disconnection()
                    return None, ErrorCode.CAMERA_DISCONNECTED
                self._report_error(ErrorCode.CAMERA_GRAB_TIMEOUT, error_msg)
                return None, ErrorCode.CAMERA_GRAB_TIMEOUT

    #========== 查询功能 ==========

    def get_parameters(self) -> Optional[CameraParameters]:
        """
        获取当前相机参数

        Returns:
            相机参数数据类，失败返回None
        """
        with self._lock:
            if not self.is_connected or self._camera is None:
                logger.error("相机未连接，无法获取参数")
                return None

            try:
                params = CameraParameters(
                    exposure_time=self._camera.ExposureTime.Value,
                    gain=self._camera.Gain.Value,
                    width=self._camera.Width.Value,
                    height=self._camera.Height.Value,
                    offset_x=self._camera.OffsetX.Value,
                    offset_y=self._camera.OffsetY.Value,
                    exposure_mode=self._camera.ExposureAuto.Value if hasattr(self._camera, 'ExposureAuto') else "Unknown",
                    white_balance_mode=self._camera.BalanceWhiteAuto.Value if hasattr(self._camera, 'BalanceWhiteAuto') else "Unknown"
                )
                return params

            except Exception as e:
                logger.error(f"获取参数失败: {e}")
                return None

    def _ensure_converter(self) -> None:
        """确保图像格式转换器已初始化"""
        if self._converter is None:
            self._converter = pylon.ImageFormatConverter()
            self._converter.OutputPixelFormat = pylon.PixelType_BGR8packed
            self._converter.OutputBitAlignment = pylon.OutputBitAlignment_MsbAligned

    def _convert_grab_result(self, grab_result) -> np.ndarray:
        """
        将抓取结果转换为BGR数组
        """
        if self._use_converter and self._converter is not None:
            try:
                converted = self._converter.Convert(grab_result)
                if hasattr(converted, 'GetArray'):
                    return converted.GetArray().copy()
                if hasattr(converted, 'Array'):
                    return converted.Array.copy()
            except Exception as e:
                logger.warning(f"图像格式转换失败，回退到原始数据: {e}")

        return grab_result.Array.copy()

    def get_supported_resolutions(self) -> List[Tuple[int, int]]:
        """
        获取支持的分辨率列表

        Returns:
            分辨率列表 [(width, height), ...]
        """
        with self._lock:
            if not self.is_connected or self._camera is None:
                logger.error("相机未连接，无法获取分辨率列表")
                return []

            try:
                max_width = self._camera.Width.Max
                max_height = self._camera.Height.Max

                #返回常用分辨率（不超过最大值）
                common_resolutions = [
                    (5472, 3648),  #全分辨率（acA5472-gc）
                    (4096, 2160),  #4K
                    (3840, 2160),  #4K UHD
                    (2736, 1824),  #半分辨率
                    (1920, 1080),  #1080p
                    (1280, 720),   #720p
                    (640, 480),    #VGA
                ]

                supported = []
                for w, h in common_resolutions:
                    if w <= max_width and h <= max_height:
                        supported.append((w, h))

                #添加最大分辨率（如果不在列表中）
                if (max_width, max_height) not in supported:
                    supported.insert(0, (max_width, max_height))

                return supported

            except Exception as e:
                logger.error(f"获取分辨率列表失败: {e}")
                return []

    def get_exposure_range(self) -> Tuple[float, float]:
        """
        获取曝光时间范围

        Returns:
            (最小值, 最大值)，单位微秒
        """
        with self._lock:
            if not self.is_connected or self._camera is None:
                return (0, 0)

            try:
                return (self._camera.ExposureTime.Min, self._camera.ExposureTime.Max)
            except Exception as e:
                logger.error(f"获取曝光范围失败: {e}")
                return (0, 0)

    def get_gain_range(self) -> Tuple[float, float]:
        """
        获取增益范围

        Returns:
            (最小值, 最大值)
        """
        with self._lock:
            if not self.is_connected or self._camera is None:
                return (0, 0)

            try:
                return (self._camera.Gain.Min, self._camera.Gain.Max)
            except Exception as e:
                logger.error(f"获取增益范围失败: {e}")
                return (0, 0)

    def get_frame_rate_range(self) -> Tuple[float, float]:
        """
        获取帧率范围

        Returns:
            (最小值, 最大值)
        """
        with self._lock:
            if not self.is_connected or self._camera is None:
                return (0, 0)

            if not hasattr(self._camera, 'AcquisitionFrameRate'):
                return (0, 0)

            try:
                return (self._camera.AcquisitionFrameRate.Min, self._camera.AcquisitionFrameRate.Max)
            except Exception as e:
                logger.error(f"获取帧率范围失败: {e}")
                return (0, 0)

    def get_supported_pixel_formats(self) -> List[str]:
        """
        获取支持的像素格式列表

        Returns:
            像素格式名称列表
        """
        with self._lock:
            if not self.is_connected or self._camera is None:
                return []

            if not hasattr(self._camera, 'PixelFormat'):
                return []

            try:
                return list(self._camera.PixelFormat.Symbolics)
            except Exception as e:
                logger.error(f"获取像素格式列表失败: {e}")
                return []

    def get_status(self) -> Dict[str, Any]:
        """
        获取相机状态信息

        Returns:
            状态字典
        """
        status = {
            "state": self._state.name,
            "state_code": self._state.value,
            "connected": self.is_connected,
            "model": "",
            "serial": "",
            "temperature": 0.0,
        }

        if self.is_connected and self._camera:
            try:
                device_info = self._camera.GetDeviceInfo()
                status["model"] = device_info.GetModelName()
                status["serial"] = device_info.GetSerialNumber()

                #尝试获取温度（部分相机支持）
                if hasattr(self._camera, 'DeviceTemperature'):
                    status["temperature"] = self._camera.DeviceTemperature.Value
            except Exception as e:
                logger.debug(f"获取部分状态信息失败: {e}")

        return status

    def __del__(self):
        """析构函数，确保资源释放"""
        self.disconnect()
