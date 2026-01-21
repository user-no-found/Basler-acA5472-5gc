# -*- coding: utf-8 -*-
"""
性能优化工具模块

提供：
- 图像缓冲池（减少内存分配）
- 网络拥塞检测
- 动态质量调整
- 性能统计
"""
import threading
import time
from typing import Optional, List, Tuple, Callable
from collections import deque
from dataclasses import dataclass, field

import numpy as np
from loguru import logger


#========== 图像缓冲池 ==========

class ImageBufferPool:
    """
    图像缓冲池

    预分配固定数量的numpy数组，减少运行时内存分配开销
    使用对象池模式，支持线程安全的获取和释放
    """

    def __init__(self, pool_size: int, buffer_shape: Tuple[int, ...], dtype=np.uint8):
        """
        初始化缓冲池

        Args:
            pool_size: 池大小（缓冲区数量）
            buffer_shape: 缓冲区形状，如(1080, 1920, 3)
            dtype: 数据类型，默认uint8
        """
        self._pool_size = pool_size
        self._buffer_shape = buffer_shape
        self._dtype = dtype

        #预分配缓冲区
        self._buffers: List[np.ndarray] = []
        self._available: deque = deque()
        self._in_use: set = set()
        self._lock = threading.Lock()

        #预分配所有缓冲区
        for i in range(pool_size):
            buffer = np.empty(buffer_shape, dtype=dtype)
            self._buffers.append(buffer)
            self._available.append(i)

        #统计信息
        self._acquire_count = 0
        self._release_count = 0
        self._miss_count = 0  #池耗尽次数

        logger.info(f"图像缓冲池初始化: 大小={pool_size}, 形状={buffer_shape}, 类型={dtype}")

    def acquire(self) -> Optional[np.ndarray]:
        """
        获取一个缓冲区

        Returns:
            np.ndarray: 缓冲区，池耗尽时返回None
        """
        with self._lock:
            if not self._available:
                self._miss_count += 1
                logger.warning(f"缓冲池耗尽，miss次数: {self._miss_count}")
                return None

            idx = self._available.popleft()
            self._in_use.add(idx)
            self._acquire_count += 1
            return self._buffers[idx]

    def release(self, buffer: np.ndarray) -> bool:
        """
        释放缓冲区回池

        Args:
            buffer: 要释放的缓冲区

        Returns:
            bool: 是否释放成功
        """
        with self._lock:
            #查找缓冲区索引
            try:
                idx = None
                for i, buf in enumerate(self._buffers):
                    if buf is buffer:
                        idx = i
                        break

                if idx is None:
                    logger.warning("尝试释放不属于池的缓冲区")
                    return False

                if idx not in self._in_use:
                    logger.warning(f"缓冲区 {idx} 未在使用中")
                    return False

                self._in_use.remove(idx)
                self._available.append(idx)
                self._release_count += 1
                return True

            except Exception as e:
                logger.error(f"释放缓冲区失败: {e}")
                return False

    def acquire_or_create(self) -> np.ndarray:
        """
        获取缓冲区，池耗尽时创建新的

        Returns:
            np.ndarray: 缓冲区（可能是池外新建的）
        """
        buffer = self.acquire()
        if buffer is None:
            #池耗尽，创建临时缓冲区
            logger.debug("池耗尽，创建临时缓冲区")
            return np.empty(self._buffer_shape, dtype=self._dtype)
        return buffer

    def clear(self) -> None:
        """清空池，释放所有缓冲区"""
        with self._lock:
            self._buffers.clear()
            self._available.clear()
            self._in_use.clear()
        logger.info("缓冲池已清空")

    def resize(self, new_shape: Tuple[int, ...]) -> None:
        """
        调整缓冲区形状

        Args:
            new_shape: 新的缓冲区形状
        """
        with self._lock:
            if new_shape == self._buffer_shape:
                return

            self._buffer_shape = new_shape
            self._buffers.clear()
            self._available.clear()
            self._in_use.clear()

            #重新分配
            for i in range(self._pool_size):
                buffer = np.empty(new_shape, dtype=self._dtype)
                self._buffers.append(buffer)
                self._available.append(i)

        logger.info(f"缓冲池已调整大小: {new_shape}")

    @property
    def available_count(self) -> int:
        """可用缓冲区数量"""
        with self._lock:
            return len(self._available)

    @property
    def in_use_count(self) -> int:
        """使用中缓冲区数量"""
        with self._lock:
            return len(self._in_use)

    def get_statistics(self) -> dict:
        """获取统计信息"""
        with self._lock:
            return {
                "pool_size": self._pool_size,
                "buffer_shape": self._buffer_shape,
                "available": len(self._available),
                "in_use": len(self._in_use),
                "acquire_count": self._acquire_count,
                "release_count": self._release_count,
                "miss_count": self._miss_count,
            }


#========== 网络拥塞检测器 ==========

@dataclass
class CongestionState:
    """拥塞状态"""
    is_congested: bool = False          #是否拥塞
    congestion_level: float = 0.0       #拥塞程度(0-1)
    recommended_quality: int = 80       #建议JPEG质量
    recommended_skip: int = 0           #建议跳帧数
    send_queue_size: int = 0            #发送队列大小


class CongestionDetector:
    """
    网络拥塞检测器

    通过监控发送延迟和队列大小来检测网络拥塞
    提供动态质量调整和跳帧建议
    """

    def __init__(self,
                 latency_threshold_ms: float = 100.0,
                 queue_threshold: int = 5,
                 history_size: int = 30):
        """
        初始化拥塞检测器

        Args:
            latency_threshold_ms: 延迟阈值（毫秒）
            queue_threshold: 队列大小阈值
            history_size: 历史记录大小
        """
        self._latency_threshold = latency_threshold_ms / 1000.0  #转换为秒
        self._queue_threshold = queue_threshold
        self._history_size = history_size

        #延迟历史
        self._latency_history: deque = deque(maxlen=history_size)

        #发送时间戳记录
        self._send_timestamps: dict = {}
        self._next_seq = 0

        #当前状态
        self._state = CongestionState()
        self._lock = threading.Lock()

        #质量调整参数
        self._min_quality = 30
        self._max_quality = 90
        self._quality_step = 10

        logger.info(f"拥塞检测器初始化: 延迟阈值={latency_threshold_ms}ms, 队列阈值={queue_threshold}")

    def record_send(self, seq: int = None) -> int:
        """
        记录发送时间

        Args:
            seq: 序号，为None时自动生成

        Returns:
            int: 序号
        """
        with self._lock:
            if seq is None:
                seq = self._next_seq
                self._next_seq += 1

            self._send_timestamps[seq] = time.perf_counter()

            #清理过期记录（超过5秒）
            current = time.perf_counter()
            expired = [k for k, v in self._send_timestamps.items() if current - v > 5.0]
            for k in expired:
                del self._send_timestamps[k]

            return seq

    def record_ack(self, seq: int) -> Optional[float]:
        """
        记录确认时间，计算延迟

        Args:
            seq: 序号

        Returns:
            float: 延迟（秒），未找到记录返回None
        """
        with self._lock:
            if seq not in self._send_timestamps:
                return None

            send_time = self._send_timestamps.pop(seq)
            latency = time.perf_counter() - send_time
            self._latency_history.append(latency)

            #更新状态
            self._update_state()

            return latency

    def update_queue_size(self, size: int) -> None:
        """
        更新发送队列大小

        Args:
            size: 队列大小
        """
        with self._lock:
            self._state.send_queue_size = size
            self._update_state()

    def _update_state(self) -> None:
        """更新拥塞状态"""
        #计算平均延迟
        if self._latency_history:
            avg_latency = sum(self._latency_history) / len(self._latency_history)
        else:
            avg_latency = 0

        #计算拥塞程度
        latency_factor = min(1.0, avg_latency / self._latency_threshold) if self._latency_threshold > 0 else 0
        queue_factor = min(1.0, self._state.send_queue_size / self._queue_threshold) if self._queue_threshold > 0 else 0

        #综合拥塞程度（延迟权重0.6，队列权重0.4）
        self._state.congestion_level = latency_factor * 0.6 + queue_factor * 0.4
        self._state.is_congested = self._state.congestion_level > 0.5

        #计算建议质量
        quality_range = self._max_quality - self._min_quality
        self._state.recommended_quality = int(
            self._max_quality - self._state.congestion_level * quality_range
        )

        #计算建议跳帧数
        if self._state.congestion_level > 0.8:
            self._state.recommended_skip = 2
        elif self._state.congestion_level > 0.5:
            self._state.recommended_skip = 1
        else:
            self._state.recommended_skip = 0

    def get_state(self) -> CongestionState:
        """获取当前拥塞状态"""
        with self._lock:
            return CongestionState(
                is_congested=self._state.is_congested,
                congestion_level=self._state.congestion_level,
                recommended_quality=self._state.recommended_quality,
                recommended_skip=self._state.recommended_skip,
                send_queue_size=self._state.send_queue_size,
            )

    def should_skip_frame(self, frame_seq: int) -> bool:
        """
        判断是否应该跳过当前帧

        Args:
            frame_seq: 帧序号

        Returns:
            bool: 是否跳过
        """
        with self._lock:
            if self._state.recommended_skip == 0:
                return False
            #每N帧跳过1帧
            return frame_seq % (self._state.recommended_skip + 1) != 0

    def reset(self) -> None:
        """重置检测器状态"""
        with self._lock:
            self._latency_history.clear()
            self._send_timestamps.clear()
            self._state = CongestionState()
        logger.info("拥塞检测器已重置")


#========== 性能统计器 ==========

@dataclass
class PerformanceMetrics:
    """性能指标"""
    fps: float = 0.0                    #实际帧率
    frame_time_ms: float = 0.0          #平均帧处理时间
    encode_time_ms: float = 0.0         #平均编码时间
    send_time_ms: float = 0.0           #平均发送时间
    memory_mb: float = 0.0              #内存使用
    dropped_frames: int = 0             #丢帧数
    total_frames: int = 0               #总帧数


class PerformanceMonitor:
    """
    性能监控器

    收集和统计各项性能指标
    """

    def __init__(self, window_size: int = 60):
        """
        初始化性能监控器

        Args:
            window_size: 统计窗口大小（帧数）
        """
        self._window_size = window_size

        #时间记录
        self._frame_times: deque = deque(maxlen=window_size)
        self._encode_times: deque = deque(maxlen=window_size)
        self._send_times: deque = deque(maxlen=window_size)

        #帧计数
        self._total_frames = 0
        self._dropped_frames = 0

        #FPS计算
        self._fps_start_time = time.perf_counter()
        self._fps_frame_count = 0
        self._current_fps = 0.0

        self._lock = threading.Lock()

        logger.info(f"性能监控器初始化: 窗口大小={window_size}")

    def record_frame_time(self, duration_ms: float) -> None:
        """记录帧处理时间"""
        with self._lock:
            self._frame_times.append(duration_ms)
            self._total_frames += 1
            self._fps_frame_count += 1

            #每秒更新FPS
            elapsed = time.perf_counter() - self._fps_start_time
            if elapsed >= 1.0:
                self._current_fps = self._fps_frame_count / elapsed
                self._fps_frame_count = 0
                self._fps_start_time = time.perf_counter()

    def record_encode_time(self, duration_ms: float) -> None:
        """记录编码时间"""
        with self._lock:
            self._encode_times.append(duration_ms)

    def record_send_time(self, duration_ms: float) -> None:
        """记录发送时间"""
        with self._lock:
            self._send_times.append(duration_ms)

    def record_dropped_frame(self) -> None:
        """记录丢帧"""
        with self._lock:
            self._dropped_frames += 1

    def get_metrics(self) -> PerformanceMetrics:
        """获取性能指标"""
        with self._lock:
            return PerformanceMetrics(
                fps=self._current_fps,
                frame_time_ms=sum(self._frame_times) / len(self._frame_times) if self._frame_times else 0,
                encode_time_ms=sum(self._encode_times) / len(self._encode_times) if self._encode_times else 0,
                send_time_ms=sum(self._send_times) / len(self._send_times) if self._send_times else 0,
                memory_mb=0,  #需要外部设置
                dropped_frames=self._dropped_frames,
                total_frames=self._total_frames,
            )

    def reset(self) -> None:
        """重置统计"""
        with self._lock:
            self._frame_times.clear()
            self._encode_times.clear()
            self._send_times.clear()
            self._total_frames = 0
            self._dropped_frames = 0
            self._fps_frame_count = 0
            self._fps_start_time = time.perf_counter()
            self._current_fps = 0.0
        logger.info("性能监控器已重置")


#========== 图像处理优化函数 ==========

def fast_bgr_to_rgb(image: np.ndarray, out: np.ndarray = None) -> np.ndarray:
    """
    快速BGR转RGB（原地操作或使用预分配缓冲区）

    Args:
        image: BGR图像
        out: 输出缓冲区，为None时创建新数组

    Returns:
        np.ndarray: RGB图像
    """
    if out is None:
        #使用numpy切片反转，避免复制
        return image[:, :, ::-1]
    else:
        #使用预分配缓冲区
        np.copyto(out[:, :, 0], image[:, :, 2])
        np.copyto(out[:, :, 1], image[:, :, 1])
        np.copyto(out[:, :, 2], image[:, :, 0])
        return out


def fast_resize_nearest(image: np.ndarray,
                        target_width: int,
                        target_height: int,
                        out: np.ndarray = None) -> np.ndarray:
    """
    快速最近邻缩放（纯numpy实现）

    适用于预览等对质量要求不高的场景

    Args:
        image: 原始图像
        target_width: 目标宽度
        target_height: 目标高度
        out: 输出缓冲区

    Returns:
        np.ndarray: 缩放后的图像
    """
    h, w = image.shape[:2]

    if w == target_width and h == target_height:
        return image

    #计算缩放比例
    x_ratio = w / target_width
    y_ratio = h / target_height

    #生成索引
    x_indices = (np.arange(target_width) * x_ratio).astype(np.int32)
    y_indices = (np.arange(target_height) * y_ratio).astype(np.int32)

    #限制索引范围
    x_indices = np.clip(x_indices, 0, w - 1)
    y_indices = np.clip(y_indices, 0, h - 1)

    #使用高级索引进行缩放
    if out is not None and out.shape[:2] == (target_height, target_width):
        np.copyto(out, image[np.ix_(y_indices, x_indices)])
        return out
    else:
        return image[np.ix_(y_indices, x_indices)]


def apply_brightness_contrast(image: np.ndarray,
                              brightness: float = 0.0,
                              contrast: float = 1.0,
                              out: np.ndarray = None) -> np.ndarray:
    """
    快速亮度对比度调整（向量化操作）

    Args:
        image: 输入图像
        brightness: 亮度调整(-255到255)
        contrast: 对比度调整(0到3)
        out: 输出缓冲区

    Returns:
        np.ndarray: 调整后的图像
    """
    if brightness == 0.0 and contrast == 1.0:
        return image

    #向量化计算
    result = image.astype(np.float32)
    result = result * contrast + brightness

    #裁剪到有效范围
    np.clip(result, 0, 255, out=result)

    if out is not None:
        np.copyto(out, result.astype(np.uint8))
        return out
    else:
        return result.astype(np.uint8)
