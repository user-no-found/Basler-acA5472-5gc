# -*- coding: utf-8 -*-
"""
照片目录监控模块

负责：
- 使用 watchdog 监控图像保存目录
- 检测到新的 JPG/JPEG 文件创建或重命名事件后，调用 exif_writer 写入 EXIF
- 使用已处理文件集合防止重复处理

依赖：
- watchdog >= 3.0.0
"""

import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

from loguru import logger

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileSystemEvent
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False
    logger.warning("watchdog 未安装，照片目录监控功能不可用")

import exif_writer

# 全局控制标志
_observer: Optional[Observer] = None
_monitor_thread: Optional[threading.Thread] = None
_should_exit = threading.Event()


class _PhotoEventHandler(FileSystemEventHandler):
    """处理照片目录中的文件系统事件"""

    def __init__(self, max_processed: int = 100):
        super().__init__()
        self._processed_files: deque[str] = deque(maxlen=max_processed)
        self._lock = threading.Lock()

    def on_created(self, event: FileSystemEvent) -> None:
        self._handle_event(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        # 重命名到最终名称的事件，检查 dest_path
        self._handle_path(event.dest_path)

    def _handle_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._handle_path(event.src_path)

    def _handle_path(self, path: str) -> None:
        file_path = Path(path)
        logger.debug(f"检查文件: {file_path}, 扩展名: {file_path.suffix}")

        # 检查扩展名（不区分大小写）
        ext = file_path.suffix.lower()
        if ext not in (".jpg", ".jpeg"):
            return

        with self._lock:
            try:
                resolved = str(file_path.resolve())
            except OSError:
                resolved = str(file_path)
            if resolved in self._processed_files:
                logger.debug(f"文件已处理过，跳过: {file_path}")
                return
            self._processed_files.append(resolved)

        logger.debug(f"检测到照片文件事件: {file_path}")
        # 在新线程中调用 ExifTool，避免阻塞 watchdog 的事件循环
        threading.Thread(
            target=exif_writer.modify_photo_exif,
            args=(file_path,),
            daemon=True,
            name=f"ExifWriter-{file_path.name}"
        ).start()


def start_photo_monitor(photo_path: str) -> bool:
    """
    启动照片目录监控

    Args:
        photo_path: 要监控的照片保存目录

    Returns:
        bool: 是否成功启动
    """
    global _observer, _monitor_thread

    if not WATCHDOG_AVAILABLE:
        logger.error("watchdog 未安装，无法启动照片目录监控")
        return False

    if _observer is not None:
        logger.warning("照片目录监控已在运行")
        return True

    path = Path(photo_path)
    if not path.exists():
        logger.error(f"照片目录不存在: {photo_path}")
        return False
    if not path.is_dir():
        logger.error(f"指定路径不是目录: {photo_path}")
        return False

    _should_exit.clear()
    event_handler = _PhotoEventHandler()
    _observer = Observer()
    _observer.schedule(event_handler, str(path), recursive=True)
    _observer.start()
    logger.info(f"启动照片目录监控: {photo_path}")
    return True


def stop_photo_monitor() -> None:
    """停止照片目录监控"""
    global _observer, _monitor_thread

    _should_exit.set()

    if _observer is not None:
        _observer.stop()
        _observer.join(timeout=3.0)
        _observer = None
        logger.info("照片目录监控已停止")

    _monitor_thread = None
