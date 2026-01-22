#-*- coding: utf-8 -*-
"""
预览显示组件模块

功能:
- 使用Canvas显示JPEG图像
- 自动缩放适配窗口大小
- 帧率统计
- 自动丢弃过时帧（仅显示最新帧）
- 帧序号检查

0xC0预览帧数据格式:
数据段: [序号(4字节大端)][JPEG长度(4字节大端)][JPEG数据]
"""

import tkinter as tk
from tkinter import ttk
import struct
import time
import io
import threading
from typing import Optional, Tuple
from loguru import logger

try:
    from PIL import Image, ImageTk
except ImportError:
    logger.error("缺少Pillow库，请执行: pip install Pillow")
    raise


class PreviewWidget(tk.Frame):
    """
    预览显示组件

    用于显示实时预览图像，支持JPEG解码、自动缩放、帧率统计
    """

    def __init__(self, parent, **kwargs):
        """
        初始化预览组件

        Args:
            parent: 父容器
            **kwargs: Frame的其他参数
        """
        super().__init__(parent, **kwargs)

        #帧管理
        self._last_frame_seq = -1  #上一帧序号
        self._frame_count = 0      #接收帧数
        self._display_count = 0    #显示帧数
        self._dropped_count = 0    #丢弃帧数

        #帧率统计
        self._fps = 0.0
        self._fps_timestamps = []  #最近帧的时间戳列表
        self._fps_window = 1.0     #帧率计算窗口（秒）

        #图像缓存
        self._current_image: Optional[ImageTk.PhotoImage] = None
        self._image_size: Tuple[int, int] = (0, 0)  #原始图像尺寸

        #线程锁
        self._lock = threading.Lock()

        #创建UI
        self._create_ui()

        #绑定窗口大小变化事件
        self.bind('<Configure>', self._on_resize)

    def _create_ui(self):
        """创建用户界面"""
        #预览画布
        self.canvas = tk.Canvas(self, bg='black', highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        #信息栏
        info_frame = ttk.Frame(self)
        info_frame.pack(fill=tk.X, pady=(2, 0))

        #帧率显示
        ttk.Label(info_frame, text="帧率:").pack(side=tk.LEFT)
        self.fps_label = ttk.Label(info_frame, text="0.0 fps", width=10)
        self.fps_label.pack(side=tk.LEFT, padx=(0, 10))

        #帧数显示
        ttk.Label(info_frame, text="帧数:").pack(side=tk.LEFT)
        self.frame_count_label = ttk.Label(info_frame, text="0", width=8)
        self.frame_count_label.pack(side=tk.LEFT, padx=(0, 10))

        #丢弃帧数
        ttk.Label(info_frame, text="丢弃:").pack(side=tk.LEFT)
        self.dropped_label = ttk.Label(info_frame, text="0", width=6)
        self.dropped_label.pack(side=tk.LEFT, padx=(0, 10))

        #分辨率显示
        ttk.Label(info_frame, text="分辨率:").pack(side=tk.LEFT)
        self.resolution_label = ttk.Label(info_frame, text="--", width=12)
        self.resolution_label.pack(side=tk.LEFT)

    def update_frame(self, jpeg_data: bytes, frame_seq: int) -> bool:
        """
        更新预览帧

        Args:
            jpeg_data: JPEG图像数据
            frame_seq: 帧序号

        Returns:
            True表示成功显示，False表示帧被丢弃或解码失败
        """
        with self._lock:
            self._frame_count += 1

            #检查帧序号，丢弃过时帧
            if frame_seq <= self._last_frame_seq:
                self._dropped_count += 1
                logger.debug(f"丢弃过时帧: seq={frame_seq}, last={self._last_frame_seq}")
                return False

            self._last_frame_seq = frame_seq

        #解码JPEG
        try:
            image = self._decode_jpeg(jpeg_data)
            if image is None:
                return False
        except Exception as e:
            logger.error(f"JPEG解码失败: {e}")
            return False

        #更新帧率统计
        self._update_fps_stats()

        #在主线程中更新显示
        self.after(0, lambda: self._display_image(image))

        return True

    def update_frame_from_protocol(self, data: bytes) -> bool:
        """
        从协议数据段更新预览帧

        解析0xC0预览帧数据格式:
        [序号(4字节大端)][JPEG长度(4字节大端)][JPEG数据]

        Args:
            data: 协议数据段

        Returns:
            True表示成功，False表示失败
        """
        result = self._parse_preview_data(data)
        if result is None:
            return False

        frame_seq, jpeg_data = result
        return self.update_frame(jpeg_data, frame_seq)

    def _parse_preview_data(self, data: bytes) -> Optional[Tuple[int, bytes]]:
        """
        解析预览帧数据

        Args:
            data: 数据段

        Returns:
            (帧序号, JPEG数据)，失败返回None
        """
        if len(data) < 8:
            logger.warning(f"预览帧数据太短: {len(data)} < 8")
            return None

        #解析帧序号（4字节大端）
        frame_seq = struct.unpack('>I', data[:4])[0]

        #解析JPEG长度（4字节大端）
        jpeg_len = struct.unpack('>I', data[4:8])[0]

        #检查数据完整性
        if len(data) < 8 + jpeg_len:
            logger.warning(f"预览帧数据不完整: 期望{8+jpeg_len}, 实际{len(data)}")
            return None

        #提取JPEG数据
        jpeg_data = data[8:8+jpeg_len]

        logger.debug(f"解析预览帧: seq={frame_seq}, jpeg_len={jpeg_len}")
        return (frame_seq, jpeg_data)

    def _decode_jpeg(self, jpeg_data: bytes) -> Optional[Image.Image]:
        """
        解码JPEG数据

        Args:
            jpeg_data: JPEG字节数据

        Returns:
            PIL Image对象，失败返回None
        """
        try:
            image = Image.open(io.BytesIO(jpeg_data))
            #确保图像已加载
            image.load()
            return image
        except Exception as e:
            logger.error(f"JPEG解码错误: {e}")
            return None

    def _display_image(self, image: Image.Image):
        """
        在画布上显示图像（主线程调用）

        Args:
            image: PIL Image对象
        """
        try:
            #获取画布尺寸
            canvas_width = self.canvas.winfo_width()
            canvas_height = self.canvas.winfo_height()

            if canvas_width <= 1 or canvas_height <= 1:
                return

            #保存原始尺寸
            self._image_size = image.size

            #计算缩放比例（保持宽高比）
            img_width, img_height = image.size
            scale_w = canvas_width / img_width
            scale_h = canvas_height / img_height
            scale = min(scale_w, scale_h)

            #缩放图像
            new_width = int(img_width * scale)
            new_height = int(img_height * scale)

            if new_width > 0 and new_height > 0:
                resized = image.resize((new_width, new_height), Image.Resampling.LANCZOS)
                self._current_image = ImageTk.PhotoImage(resized)

                #清除画布并显示图像（居中）
                self.canvas.delete('all')
                x = (canvas_width - new_width) // 2
                y = (canvas_height - new_height) // 2
                self.canvas.create_image(x, y, anchor=tk.NW, image=self._current_image)

            #更新显示计数
            with self._lock:
                self._display_count += 1

            #更新UI标签
            self._update_labels()

        except Exception as e:
            logger.error(f"显示图像失败: {e}")

    def _update_fps_stats(self):
        """更新帧率统计"""
        current_time = time.time()

        with self._lock:
            #添加当前时间戳
            self._fps_timestamps.append(current_time)

            #移除超出窗口的时间戳
            cutoff = current_time - self._fps_window
            self._fps_timestamps = [t for t in self._fps_timestamps if t > cutoff]

            #计算帧率
            if len(self._fps_timestamps) >= 2:
                time_span = self._fps_timestamps[-1] - self._fps_timestamps[0]
                if time_span > 0:
                    self._fps = (len(self._fps_timestamps) - 1) / time_span
                else:
                    self._fps = 0.0
            else:
                self._fps = 0.0

    def _update_labels(self):
        """更新UI标签"""
        with self._lock:
            fps = self._fps
            frame_count = self._frame_count
            dropped = self._dropped_count
            img_size = self._image_size

        self.fps_label.config(text=f"{fps:.1f} fps")
        self.frame_count_label.config(text=str(frame_count))
        self.dropped_label.config(text=str(dropped))

        if img_size[0] > 0 and img_size[1] > 0:
            self.resolution_label.config(text=f"{img_size[0]}x{img_size[1]}")

    def _on_resize(self, event):
        """窗口大小变化事件"""
        #如果有当前图像，重新显示以适应新尺寸
        #注意：这里不重新解码，只是触发重绘
        pass

    def clear(self):
        """清除预览显示"""
        self.canvas.delete('all')
        self._current_image = None

        with self._lock:
            self._last_frame_seq = -1
            self._frame_count = 0
            self._display_count = 0
            self._dropped_count = 0
            self._fps = 0.0
            self._fps_timestamps = []
            self._image_size = (0, 0)

        #更新标签
        self.fps_label.config(text="0.0 fps")
        self.frame_count_label.config(text="0")
        self.dropped_label.config(text="0")
        self.resolution_label.config(text="--")

        logger.info("预览显示已清除")

    def get_fps(self) -> float:
        """
        获取当前帧率

        Returns:
            当前帧率（fps）
        """
        with self._lock:
            return self._fps

    def get_frame_count(self) -> int:
        """
        获取接收帧数

        Returns:
            接收帧数
        """
        with self._lock:
            return self._frame_count

    def get_dropped_count(self) -> int:
        """
        获取丢弃帧数

        Returns:
            丢弃帧数
        """
        with self._lock:
            return self._dropped_count

    def get_display_count(self) -> int:
        """
        获取显示帧数

        Returns:
            显示帧数
        """
        with self._lock:
            return self._display_count

    def get_image_size(self) -> Tuple[int, int]:
        """
        获取当前图像尺寸

        Returns:
            (宽度, 高度)
        """
        with self._lock:
            return self._image_size


if __name__ == '__main__':
    #测试代码
    import sys
    logger.remove()
    logger.add(sys.stdout, level="DEBUG")

    #创建测试窗口
    root = tk.Tk()
    root.title("预览组件测试")
    root.geometry("800x600")

    #创建预览组件
    preview = PreviewWidget(root)
    preview.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    #测试按钮
    btn_frame = ttk.Frame(root)
    btn_frame.pack(fill=tk.X, padx=5, pady=5)

    def test_image():
        """测试显示图像"""
        #创建测试图像
        test_img = Image.new('RGB', (640, 480), color='blue')
        #添加一些内容
        from PIL import ImageDraw
        draw = ImageDraw.Draw(test_img)
        draw.rectangle([100, 100, 540, 380], fill='green')
        draw.text((250, 230), "Test Image", fill='white')

        #转换为JPEG
        buffer = io.BytesIO()
        test_img.save(buffer, format='JPEG', quality=85)
        jpeg_data = buffer.getvalue()

        #模拟帧序号
        import random
        seq = random.randint(1, 10000)

        #更新显示
        preview.update_frame(jpeg_data, seq)
        logger.info(f"测试帧已发送: seq={seq}, size={len(jpeg_data)}")

    def test_protocol():
        """测试协议数据解析"""
        #创建测试图像
        test_img = Image.new('RGB', (320, 240), color='red')

        #转换为JPEG
        buffer = io.BytesIO()
        test_img.save(buffer, format='JPEG', quality=85)
        jpeg_data = buffer.getvalue()

        #构建协议数据
        seq = 12345
        data = struct.pack('>I', seq) + struct.pack('>I', len(jpeg_data)) + jpeg_data

        #更新显示
        preview.update_frame_from_protocol(data)
        logger.info(f"协议帧已发送: seq={seq}")

    def clear_preview():
        """清除预览"""
        preview.clear()

    ttk.Button(btn_frame, text="测试图像", command=test_image).pack(side=tk.LEFT, padx=5)
    ttk.Button(btn_frame, text="测试协议", command=test_protocol).pack(side=tk.LEFT, padx=5)
    ttk.Button(btn_frame, text="清除", command=clear_preview).pack(side=tk.LEFT, padx=5)

    #显示帧率
    def update_fps_display():
        fps = preview.get_fps()
        root.title(f"预览组件测试 - {fps:.1f} fps")
        root.after(500, update_fps_display)

    update_fps_display()

    root.mainloop()
