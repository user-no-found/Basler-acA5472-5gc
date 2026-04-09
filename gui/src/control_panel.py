#-*- coding: utf-8 -*-
"""
控制面板模块

功能:
- 拍照控制（拍照按钮、显示最后拍照文件名）
- 录像控制（开始/停止、时长、分辨率、帧率、状态显示）
- 预览控制（开启/停止、分辨率、帧率、状态显示）
- 参数设置（曝光模式/值、增益、白平衡模式）
"""

import tkinter as tk
from tkinter import ttk, messagebox
from typing import Callable, Optional, Tuple
from loguru import logger
from datetime import datetime
import threading
import time

from protocol_builder import (
    build_capture, build_record_start, build_record_stop,
    build_preview_start, build_preview_stop,
    build_continuous_start, build_continuous_stop,
    build_set_exposure, build_set_gain, build_set_white_balance,
    build_set_resolution,
    build_set_gain_auto, build_set_frame_rate, build_set_pixel_format,
    build_set_flash
)
from error_codes import get_error_message


#分辨率选项
RESOLUTION_OPTIONS = [
    ("5472x3648", 0, 5472, 3648),
    ("4096x2160", 1, 4096, 2160),
    ("3840x2160", 2, 3840, 2160),
    ("2736x1824", 3, 2736, 1824),
    ("1920x1080", 4, 1920, 1080),
    ("1280x720", 5, 1280, 720),
    ("640x480", 6, 640, 480),
]

#协议中录像/预览帧率为1字节
FPS_MIN = 1
FPS_MAX = 255
FPS_OPTIONS = [str(i) for i in range(FPS_MIN, FPS_MAX + 1)]

#像素格式选项
PIXEL_FORMAT_OPTIONS = [
    ("BayerRG8", 0),
    ("BayerRG12", 1),
    ("BGR8", 2),
    ("RGB8", 3),
    ("Mono8", 4),
]


class ControlPanel(ttk.Frame):
    """控制面板组件"""

    def __init__(self, parent, send_callback: Callable[[bytes], bool]):
        """
        初始化控制面板

        Args:
            parent: 父容器
            send_callback: 发送数据回调函数
        """
        super().__init__(parent, padding="5")
        self._send = send_callback

        #状态变量
        self._is_recording = False
        self._is_previewing = False
        self._is_continuous = False
        self._last_capture_file = ""
        self._fps_min = FPS_MIN
        self._fps_max = FPS_MAX

        #闪光灯测试状态
        self._is_flash_testing = False
        self._flash_test_thread = None
        self._flash_test_stop_event = threading.Event()

        #创建界面
        self._create_ui()

    def _create_ui(self):
        """创建用户界面"""
        #拍照控制
        self._create_capture_section()

        #录像控制
        self._create_record_section()

        #预览控制
        self._create_preview_section()

        #参数设置
        self._create_params_section()

        #闪光灯控制
        self._create_flash_section()

        #闪光灯延时测试
        self._create_flash_test_section()

    def _create_capture_section(self):
        """创建拍照控制区域"""
        frame = ttk.LabelFrame(self, text="拍照控制", padding="5")
        frame.pack(fill=tk.X, pady=(0, 5))

        #拍照按钮
        self.capture_btn = ttk.Button(frame, text="拍照", command=self._on_capture)
        self.capture_btn.pack(fill=tk.X, pady=2)

        #连续拍照按钮区域
        continuous_frame = ttk.Frame(frame)
        continuous_frame.pack(fill=tk.X, pady=2)

        self.continuous_start_btn = ttk.Button(continuous_frame, text="开始连拍", command=self._on_continuous_start)
        self.continuous_start_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 2))

        self.continuous_stop_btn = ttk.Button(continuous_frame, text="停止连拍", command=self._on_continuous_stop, state=tk.DISABLED)
        self.continuous_stop_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(2, 0))

        #连续拍照状态
        continuous_status_frame = ttk.Frame(frame)
        continuous_status_frame.pack(fill=tk.X, pady=2)

        ttk.Label(continuous_status_frame, text="连拍状态:").pack(side=tk.LEFT)
        self.continuous_status_label = ttk.Label(continuous_status_frame, text="未连拍", foreground="gray")
        self.continuous_status_label.pack(side=tk.LEFT, padx=(5, 0))

        #最后拍照文件名
        file_frame = ttk.Frame(frame)
        file_frame.pack(fill=tk.X, pady=2)

        ttk.Label(file_frame, text="最后拍照:").pack(side=tk.LEFT)
        self.capture_file_label = ttk.Label(file_frame, text="--", foreground="gray")
        self.capture_file_label.pack(side=tk.LEFT, padx=(5, 0))

    def _create_record_section(self):
        """创建录像控制区域"""
        frame = ttk.LabelFrame(self, text="录像控制", padding="5")
        frame.pack(fill=tk.X, pady=(0, 5))

        #录像时长
        duration_frame = ttk.Frame(frame)
        duration_frame.pack(fill=tk.X, pady=2)

        ttk.Label(duration_frame, text="时长(秒):").pack(side=tk.LEFT)
        self.record_duration_var = tk.StringVar(value="0")
        self.record_duration_entry = ttk.Entry(duration_frame, textvariable=self.record_duration_var, width=8)
        self.record_duration_entry.pack(side=tk.LEFT, padx=(5, 0))
        ttk.Label(duration_frame, text="(0=手动停止)", foreground="gray").pack(side=tk.LEFT, padx=(5, 0))

        #分辨率选择
        res_frame = ttk.Frame(frame)
        res_frame.pack(fill=tk.X, pady=2)

        ttk.Label(res_frame, text="分辨率:").pack(side=tk.LEFT)
        self.record_res_var = tk.StringVar(value=RESOLUTION_OPTIONS[0][0])
        self.record_res_combo = ttk.Combobox(
            res_frame,
            textvariable=self.record_res_var,
            values=[r[0] for r in RESOLUTION_OPTIONS],
            state="readonly",
            width=12
        )
        self.record_res_combo.pack(side=tk.LEFT, padx=(5, 0))

        #帧率选择
        fps_frame = ttk.Frame(frame)
        fps_frame.pack(fill=tk.X, pady=2)

        ttk.Label(fps_frame, text="帧率:").pack(side=tk.LEFT)
        self.record_fps_var = tk.StringVar(value="5")
        self.record_fps_combo = ttk.Combobox(
            fps_frame,
            textvariable=self.record_fps_var,
            values=FPS_OPTIONS,
            state="readonly",
            width=6
        )
        self.record_fps_combo.pack(side=tk.LEFT, padx=(5, 0))
        ttk.Label(fps_frame, text="fps").pack(side=tk.LEFT, padx=(2, 0))

        #按钮区域
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X, pady=2)

        self.record_start_btn = ttk.Button(btn_frame, text="开始录像", command=self._on_record_start)
        self.record_start_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 2))

        self.record_stop_btn = ttk.Button(btn_frame, text="停止录像", command=self._on_record_stop, state=tk.DISABLED)
        self.record_stop_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(2, 0))

        #录像状态
        status_frame = ttk.Frame(frame)
        status_frame.pack(fill=tk.X, pady=2)

        ttk.Label(status_frame, text="状态:").pack(side=tk.LEFT)
        self.record_status_label = ttk.Label(status_frame, text="未录像", foreground="gray")
        self.record_status_label.pack(side=tk.LEFT, padx=(5, 0))

        rec_fps_frame = ttk.Frame(frame)
        rec_fps_frame.pack(fill=tk.X, pady=2)
        ttk.Label(rec_fps_frame, text="REC FPS:").pack(side=tk.LEFT)
        self.record_live_fps_label = ttk.Label(rec_fps_frame, text="--", foreground="gray")
        self.record_live_fps_label.pack(side=tk.LEFT, padx=(5, 0))

    def _create_preview_section(self):
        """创建预览控制区域"""
        frame = ttk.LabelFrame(self, text="预览控制", padding="5")
        frame.pack(fill=tk.X, pady=(0, 5))

        #分辨率选择
        res_frame = ttk.Frame(frame)
        res_frame.pack(fill=tk.X, pady=2)

        ttk.Label(res_frame, text="分辨率:").pack(side=tk.LEFT)
        self.preview_res_var = tk.StringVar(value=RESOLUTION_OPTIONS[-1][0])
        self.preview_res_combo = ttk.Combobox(
            res_frame,
            textvariable=self.preview_res_var,
            values=[r[0] for r in RESOLUTION_OPTIONS],
            state="readonly",
            width=12
        )
        self.preview_res_combo.pack(side=tk.LEFT, padx=(5, 0))

        #帧率选择
        fps_frame = ttk.Frame(frame)
        fps_frame.pack(fill=tk.X, pady=2)

        ttk.Label(fps_frame, text="帧率:").pack(side=tk.LEFT)
        self.preview_fps_var = tk.StringVar(value="10")
        self.preview_fps_combo = ttk.Combobox(
            fps_frame,
            textvariable=self.preview_fps_var,
            values=FPS_OPTIONS,
            state="readonly",
            width=6
        )
        self.preview_fps_combo.pack(side=tk.LEFT, padx=(5, 0))
        ttk.Label(fps_frame, text="fps").pack(side=tk.LEFT, padx=(2, 0))

        #按钮区域
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X, pady=2)

        self.preview_start_btn = ttk.Button(btn_frame, text="开启预览", command=self._on_preview_start)
        self.preview_start_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 2))

        self.preview_stop_btn = ttk.Button(btn_frame, text="停止预览", command=self._on_preview_stop, state=tk.DISABLED)
        self.preview_stop_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(2, 0))

        #预览状态
        status_frame = ttk.Frame(frame)
        status_frame.pack(fill=tk.X, pady=2)

        ttk.Label(status_frame, text="状态:").pack(side=tk.LEFT)
        self.preview_status_label = ttk.Label(status_frame, text="未预览", foreground="gray")
        self.preview_status_label.pack(side=tk.LEFT, padx=(5, 0))

    def _create_params_section(self):
        """创建参数设置区域"""
        frame = ttk.LabelFrame(self, text="参数设置", padding="5")
        frame.pack(fill=tk.X, pady=(0, 5))

        #分辨率设置
        res_frame = ttk.Frame(frame)
        res_frame.pack(fill=tk.X, pady=2)

        ttk.Label(res_frame, text="分辨率:").pack(side=tk.LEFT)
        self.param_res_var = tk.StringVar(value=RESOLUTION_OPTIONS[0][0])
        self.param_res_combo = ttk.Combobox(
            res_frame,
            textvariable=self.param_res_var,
            values=[r[0] for r in RESOLUTION_OPTIONS],
            state="readonly",
            width=12
        )
        self.param_res_combo.pack(side=tk.LEFT, padx=(5, 0))

        #曝光模式
        exp_mode_frame = ttk.Frame(frame)
        exp_mode_frame.pack(fill=tk.X, pady=2)

        ttk.Label(exp_mode_frame, text="曝光模式:").pack(side=tk.LEFT)
        self.exposure_mode_var = tk.StringVar(value="自动")
        self.exposure_mode_combo = ttk.Combobox(
            exp_mode_frame,
            textvariable=self.exposure_mode_var,
            values=["自动", "手动"],
            state="readonly",
            width=8
        )
        self.exposure_mode_combo.pack(side=tk.LEFT, padx=(5, 0))
        self.exposure_mode_combo.bind("<<ComboboxSelected>>", self._on_exposure_mode_changed)

        #曝光时间
        exp_val_frame = ttk.Frame(frame)
        exp_val_frame.pack(fill=tk.X, pady=2)

        ttk.Label(exp_val_frame, text="曝光时间:").pack(side=tk.LEFT)
        self.exposure_value_var = tk.StringVar(value="10000")
        self.exposure_value_entry = ttk.Entry(exp_val_frame, textvariable=self.exposure_value_var, width=10, state=tk.DISABLED)
        self.exposure_value_entry.pack(side=tk.LEFT, padx=(5, 0))
        ttk.Label(exp_val_frame, text="us").pack(side=tk.LEFT, padx=(2, 0))

        #自动增益开关
        gain_auto_frame = ttk.Frame(frame)
        gain_auto_frame.pack(fill=tk.X, pady=2)

        self.gain_auto_var = tk.BooleanVar(value=True)
        self.gain_auto_check = ttk.Checkbutton(
            gain_auto_frame,
            text="自动增益",
            variable=self.gain_auto_var,
            command=self._on_gain_auto_changed
        )
        self.gain_auto_check.pack(side=tk.LEFT)

        #增益
        gain_frame = ttk.Frame(frame)
        gain_frame.pack(fill=tk.X, pady=2)

        ttk.Label(gain_frame, text="增益:").pack(side=tk.LEFT)
        self.gain_var = tk.StringVar(value="100")
        self.gain_entry = ttk.Entry(gain_frame, textvariable=self.gain_var, width=10, state=tk.DISABLED)
        self.gain_entry.pack(side=tk.LEFT, padx=(5, 0))
        ttk.Label(gain_frame, text="(0-1000)").pack(side=tk.LEFT, padx=(2, 0))

        #白平衡模式
        wb_frame = ttk.Frame(frame)
        wb_frame.pack(fill=tk.X, pady=2)

        ttk.Label(wb_frame, text="白平衡:").pack(side=tk.LEFT)
        self.wb_mode_var = tk.StringVar(value="连续")
        self.wb_mode_combo = ttk.Combobox(
            wb_frame,
            textvariable=self.wb_mode_var,
            values=["连续", "一次", "关闭"],
            state="readonly",
            width=8
        )
        self.wb_mode_combo.pack(side=tk.LEFT, padx=(5, 0))

        #帧率限制
        fps_limit_frame = ttk.Frame(frame)
        fps_limit_frame.pack(fill=tk.X, pady=2)

        self.fps_limit_var = tk.BooleanVar(value=False)
        self.fps_limit_check = ttk.Checkbutton(
            fps_limit_frame,
            text="帧率限制",
            variable=self.fps_limit_var,
            command=self._on_fps_limit_changed
        )
        self.fps_limit_check.pack(side=tk.LEFT)

        #帧率设置
        fps_frame = ttk.Frame(frame)
        fps_frame.pack(fill=tk.X, pady=2)

        ttk.Label(fps_frame, text="帧率:").pack(side=tk.LEFT)
        self.fps_var = tk.StringVar(value="30")
        self.fps_spinbox = ttk.Spinbox(
            fps_frame,
            textvariable=self.fps_var,
            from_=self._fps_min,
            to=self._fps_max,
            width=8,
            state=tk.DISABLED
        )
        self.fps_spinbox.pack(side=tk.LEFT, padx=(5, 0))
        ttk.Label(fps_frame, text="Hz").pack(side=tk.LEFT, padx=(2, 0))

        #像素格式选择
        pixel_format_frame = ttk.Frame(frame)
        pixel_format_frame.pack(fill=tk.X, pady=2)

        ttk.Label(pixel_format_frame, text="像素格式:").pack(side=tk.LEFT)
        self.pixel_format_var = tk.StringVar(value=PIXEL_FORMAT_OPTIONS[0][0])
        self.pixel_format_combo = ttk.Combobox(
            pixel_format_frame,
            textvariable=self.pixel_format_var,
            values=[pf[0] for pf in PIXEL_FORMAT_OPTIONS],
            state="readonly",
            width=12
        )
        self.pixel_format_combo.pack(side=tk.LEFT, padx=(5, 0))

        #应用按钮
        self.apply_params_btn = ttk.Button(frame, text="应用参数", command=self._on_apply_params)
        self.apply_params_btn.pack(fill=tk.X, pady=(5, 2))

    def _create_flash_section(self):
        """创建闪光灯控制区域"""
        frame = ttk.LabelFrame(self, text="闪光灯控制（TCP触发）", padding="5")
        frame.pack(fill=tk.X, pady=(0, 5))

        enable_frame = ttk.Frame(frame)
        enable_frame.pack(fill=tk.X, pady=2)
        self.flash_enable_var = tk.BooleanVar(value=False)
        self.flash_enable_check = ttk.Checkbutton(
            enable_frame,
            text="启用闪光输出",
            variable=self.flash_enable_var
        )
        self.flash_enable_check.pack(side=tk.LEFT)

        delay_frame = ttk.Frame(frame)
        delay_frame.pack(fill=tk.X, pady=2)
        ttk.Label(delay_frame, text="拍照延时(ms):").pack(side=tk.LEFT)
        self.flash_delay_var = tk.StringVar(value="0")
        self.flash_delay_entry = ttk.Entry(delay_frame, textvariable=self.flash_delay_var, width=10)
        self.flash_delay_entry.pack(side=tk.LEFT, padx=(5, 0))
        ttk.Label(
            delay_frame,
            text="(先发AA AA，再等待该时长后拍照)",
            foreground="gray"
        ).pack(side=tk.LEFT, padx=(5, 0))

        self.apply_flash_btn = ttk.Button(frame, text="应用闪光灯设置", command=self._on_apply_flash)
        self.apply_flash_btn.pack(fill=tk.X, pady=(5, 2))

    def _create_flash_test_section(self):
        """创建闪光灯延时测试区域"""
        frame = ttk.LabelFrame(self, text="闪光灯延时测试", padding="5")
        frame.pack(fill=tk.X, pady=(0, 5))

        #延时范围输入
        range_frame = ttk.Frame(frame)
        range_frame.pack(fill=tk.X, pady=2)

        ttk.Label(range_frame, text="起始延时:").pack(side=tk.LEFT)
        self.flash_test_start_var = tk.StringVar(value="100")
        self.flash_test_start_entry = ttk.Entry(range_frame, textvariable=self.flash_test_start_var, width=8)
        self.flash_test_start_entry.pack(side=tk.LEFT, padx=(5, 0))
        ttk.Label(range_frame, text="ms").pack(side=tk.LEFT, padx=(2, 10))

        ttk.Label(range_frame, text="结束延时:").pack(side=tk.LEFT)
        self.flash_test_end_var = tk.StringVar(value="200")
        self.flash_test_end_entry = ttk.Entry(range_frame, textvariable=self.flash_test_end_var, width=8)
        self.flash_test_end_entry.pack(side=tk.LEFT, padx=(5, 0))
        ttk.Label(range_frame, text="ms").pack(side=tk.LEFT, padx=(2, 0))

        #间隔步长输入
        step_frame = ttk.Frame(frame)
        step_frame.pack(fill=tk.X, pady=2)

        ttk.Label(step_frame, text="间隔步长:").pack(side=tk.LEFT)
        self.flash_test_step_var = tk.StringVar(value="10")
        self.flash_test_step_entry = ttk.Entry(step_frame, textvariable=self.flash_test_step_var, width=8)
        self.flash_test_step_entry.pack(side=tk.LEFT, padx=(5, 0))
        ttk.Label(step_frame, text="ms").pack(side=tk.LEFT, padx=(2, 0))

        #按钮区域
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X, pady=2)

        self.flash_test_start_btn = ttk.Button(btn_frame, text="开始测试", command=self._on_flash_test_start)
        self.flash_test_start_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 2))

        self.flash_test_stop_btn = ttk.Button(btn_frame, text="停止测试", command=self._on_flash_test_stop, state=tk.DISABLED)
        self.flash_test_stop_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(2, 0))

        #状态显示
        status_frame = ttk.Frame(frame)
        status_frame.pack(fill=tk.X, pady=2)

        ttk.Label(status_frame, text="状态:").pack(side=tk.LEFT)
        self.flash_test_status_label = ttk.Label(status_frame, text="等待开始", foreground="gray")
        self.flash_test_status_label.pack(side=tk.LEFT, padx=(5, 0))

        #进度显示
        progress_frame = ttk.Frame(frame)
        progress_frame.pack(fill=tk.X, pady=2)

        ttk.Label(progress_frame, text="当前:").pack(side=tk.LEFT)
        self.flash_test_current_label = ttk.Label(progress_frame, text="--", foreground="blue")
        self.flash_test_current_label.pack(side=tk.LEFT, padx=(5, 0))

        ttk.Label(progress_frame, text="进度:").pack(side=tk.LEFT, padx=(10, 0))
        self.flash_test_progress_label = ttk.Label(progress_frame, text="--", foreground="green")
        self.flash_test_progress_label.pack(side=tk.LEFT, padx=(5, 0))

    def _get_resolution_index(self, res_str: str) -> int:
        """获取分辨率索引"""
        for name, index, w, h in RESOLUTION_OPTIONS:
            if name == res_str:
                return index
        return 0

    def _get_resolution_size(self, res_str: str) -> Tuple[int, int]:
        """获取分辨率宽高"""
        for name, index, w, h in RESOLUTION_OPTIONS:
            if name == res_str:
                return w, h
        return RESOLUTION_OPTIONS[0][2], RESOLUTION_OPTIONS[0][3]

    def _set_param_resolution(self, width: int, height: int) -> None:
        """同步参数分辨率显示"""
        label = f"{width}x{height}"
        values = list(self.param_res_combo["values"])
        if label not in values:
            values = [label] + values
            self.param_res_combo["values"] = values
        self.param_res_var.set(label)

    def _on_exposure_mode_changed(self, event=None):
        """曝光模式变化"""
        if self.exposure_mode_var.get() == "手动":
            self.exposure_value_entry.config(state=tk.NORMAL)
        else:
            self.exposure_value_entry.config(state=tk.DISABLED)

    def _on_gain_auto_changed(self):
        """自动增益开关变化"""
        if self.gain_auto_var.get():
            #自动增益开启，禁用手动增益输入
            self.gain_entry.config(state=tk.DISABLED)
        else:
            #自动增益关闭，启用手动增益输入
            self.gain_entry.config(state=tk.NORMAL)

    def _on_fps_limit_changed(self):
        """帧率限制开关变化"""
        if self.fps_limit_var.get():
            #帧率限制开启，启用帧率输入
            self.fps_spinbox.config(state=tk.NORMAL)
        else:
            #帧率限制关闭，禁用帧率输入
            self.fps_spinbox.config(state=tk.DISABLED)

    def _get_pixel_format_index(self, format_name: str) -> int:
        """获取像素格式索引"""
        for name, index in PIXEL_FORMAT_OPTIONS:
            if name == format_name:
                return index
        return 0

    def _on_capture(self):
        """拍照按钮点击"""
        logger.info("发送拍照命令")
        self._send(build_capture())

    def _on_continuous_start(self):
        """开始连续拍照按钮点击"""
        logger.info("发送开始连续拍照命令")
        self._send(build_continuous_start())

    def _on_continuous_stop(self):
        """停止连续拍照按钮点击"""
        logger.info("发送停止连续拍照命令")
        self._send(build_continuous_stop())

    def _show_input_warning(self, field_name: str, invalid_value: str, default_value):
        """
        显示输入验证警告

        Args:
            field_name: 字段名称
            invalid_value: 非法输入值
            default_value: 使用的默认值
        """
        msg = f"输入值 '{invalid_value}' 无效，已使用默认值 {default_value}"
        logger.warning(f"{field_name}: {msg}")
        messagebox.showwarning("输入验证警告", f"{field_name}: {msg}")

    def _widget_has_focus(self, widget) -> bool:
        """
        判断指定控件或其内部子控件当前是否拥有焦点
        """
        try:
            focused = self.focus_get()
            if focused is None:
                return False
            return str(focused).startswith(str(widget))
        except Exception:
            return False

    def _clamp_fps(self, fps: float) -> float:
        """按当前帧率范围裁剪输入值"""
        return max(float(self._fps_min), min(float(self._fps_max), float(fps)))

    def _on_record_start(self):
        """开始录像按钮点击"""
        duration_str = self.record_duration_var.get()
        try:
            duration = int(duration_str)
            if duration < 0:
                duration = 0
        except ValueError:
            duration = 0
            self._show_input_warning("录像时长", duration_str, duration)

        res_index = self._get_resolution_index(self.record_res_var.get())

        fps_str = self.record_fps_var.get()
        try:
            fps = int(fps_str)
            fps = int(self._clamp_fps(fps))
        except ValueError:
            fps = 5
            self._show_input_warning("录像帧率", fps_str, fps)

        logger.info(f"发送开始录像命令: duration={duration}, res_index={res_index}, fps={fps}")
        self._send(build_record_start(duration=duration, resolution_index=res_index, fps=fps))

    def _on_record_stop(self):
        """停止录像按钮点击"""
        logger.info("发送停止录像命令")
        self._send(build_record_stop())

    def _on_preview_start(self):
        """开启预览按钮点击"""
        res_index = self._get_resolution_index(self.preview_res_var.get())

        fps_str = self.preview_fps_var.get()
        try:
            fps = int(fps_str)
            fps = int(self._clamp_fps(fps))
        except ValueError:
            fps = 10
            self._show_input_warning("预览帧率", fps_str, fps)

        logger.info(f"发送开启预览命令: res_index={res_index}, fps={fps}")
        self._send(build_preview_start(resolution_index=res_index, fps=fps))

    def _on_preview_stop(self):
        """停止预览按钮点击"""
        logger.info("发送停止预览命令")
        self._send(build_preview_stop())

    def _on_apply_params(self):
        """应用参数按钮点击"""
        #分辨率设置
        width, height = self._get_resolution_size(self.param_res_var.get())

        logger.info(f"发送分辨率设置: {width}x{height}")
        self._send(build_set_resolution(width=width, height=height))

        #曝光设置
        exp_mode = 0 if self.exposure_mode_var.get() == "自动" else 1
        exp_str = self.exposure_value_var.get()
        try:
            exp_value = int(exp_str)
            exp_value = max(0, exp_value)
        except ValueError:
            exp_value = 10000
            self._show_input_warning("曝光时间", exp_str, exp_value)

        logger.info(f"发送曝光设置: mode={exp_mode}, value={exp_value}")
        self._send(build_set_exposure(mode=exp_mode, value=exp_value))

        #自动增益设置
        gain_auto = 1 if self.gain_auto_var.get() else 0
        logger.info(f"发送自动增益设置: mode={gain_auto}")
        self._send(build_set_gain_auto(mode=gain_auto))

        #增益设置（仅在手动模式下发送）
        if not self.gain_auto_var.get():
            gain_str = self.gain_var.get()
            try:
                gain = int(gain_str)
                gain = max(0, min(1000, gain))
            except ValueError:
                gain = 100
                self._show_input_warning("增益", gain_str, gain)

            logger.info(f"发送增益设置: value={gain}")
            self._send(build_set_gain(value=gain))

        #白平衡设置
        # 0=连续(Continuous), 1=一次(Once), 2=关闭(Off)
        wb_mode_str = self.wb_mode_var.get()
        if wb_mode_str == "连续":
            wb_mode = 0
        elif wb_mode_str == "一次":
            wb_mode = 1
        else:  # 关闭
            wb_mode = 2
        logger.info(f"发送白平衡设置: mode={wb_mode} ({wb_mode_str})")
        self._send(build_set_white_balance(mode=wb_mode))

        #帧率设置
        fps_enable = self.fps_limit_var.get()
        fps_str = self.fps_var.get()
        try:
            fps_value = float(fps_str)
            fps_value = self._clamp_fps(fps_value)
        except ValueError:
            fps_value = 30.0
            self._show_input_warning("帧率", fps_str, fps_value)

        #帧率值转换为整数（帧率*100）
        fps_int = int(fps_value * 100)
        logger.info(f"发送帧率设置: enable={fps_enable}, fps={fps_value}")
        self._send(build_set_frame_rate(fps=fps_int, enable=fps_enable))

        #像素格式设置
        pixel_format_index = self._get_pixel_format_index(self.pixel_format_var.get())
        logger.info(f"发送像素格式设置: format_index={pixel_format_index}")
        self._send(build_set_pixel_format(format_index=pixel_format_index))

    def _on_apply_flash(self):
        """应用闪光灯参数"""
        enable = self.flash_enable_var.get()

        delay_str = self.flash_delay_var.get().strip()

        try:
            delay_ms = int(delay_str)
            delay_ms = max(0, delay_ms)
        except ValueError:
            delay_ms = 0
            self._show_input_warning("闪光后拍照延时(ms)", delay_str, delay_ms)

        logger.info(
            f"发送闪光灯设置: enable={enable}, delay={delay_ms}ms"
        )
        self._send(
            build_set_flash(
                enable=enable,
                delay_ms=delay_ms
            )
        )

    def _on_flash_test_start(self):
        """开始闪光灯延时测试"""
        if self._is_flash_testing:
            return

        #获取测试参数
        try:
            start_delay = int(self.flash_test_start_var.get().strip())
            end_delay = int(self.flash_test_end_var.get().strip())
            step = int(self.flash_test_step_var.get().strip())

            if start_delay < 0 or end_delay < 0 or step <= 0:
                messagebox.showerror("参数错误", "延时值必须≥0，间隔必须>0")
                return

            if start_delay > end_delay:
                messagebox.showerror("参数错误", "起始延时不能大于结束延时")
                return

        except ValueError:
            messagebox.showerror("参数错误", "请输入有效的数字")
            return

        #计算总张数
        total_count = (end_delay - start_delay) // step + 1

        #更新UI状态
        self._is_flash_testing = True
        self._flash_test_stop_event.clear()
        self.flash_test_start_btn.config(state=tk.DISABLED)
        self.flash_test_stop_btn.config(state=tk.NORMAL)
        self.flash_test_status_label.config(text="测试中", foreground="orange")

        #启动测试线程
        self._flash_test_thread = threading.Thread(
            target=self._flash_test_worker,
            args=(start_delay, end_delay, step, total_count),
            daemon=True
        )
        self._flash_test_thread.start()

        logger.info(f"开始闪光灯延时测试: {start_delay}ms -> {end_delay}ms, 步长{step}ms, 共{total_count}张")

    def _on_flash_test_stop(self):
        """停止闪光灯延时测试"""
        if not self._is_flash_testing:
            return

        self._flash_test_stop_event.set()
        self.flash_test_status_label.config(text="停止中", foreground="red")
        logger.info("停止闪光灯延时测试")

    def _flash_test_worker(self, start_delay: int, end_delay: int, step: int, total_count: int):
        """
        闪光灯测试工作线程

        Args:
            start_delay: 起始延时(ms)
            end_delay: 结束延时(ms)
            step: 间隔步长(ms)
            total_count: 总张数
        """
        current_delay = start_delay
        current_count = 0

        try:
            while current_delay <= end_delay and not self._flash_test_stop_event.is_set():
                current_count += 1

                #更新UI显示
                self.after(0, lambda d=current_delay, c=current_count, t=total_count: self._update_flash_test_ui(d, c, t))

                #设置闪光灯延时
                logger.info(f"闪光灯测试: 第{current_count}/{total_count}张, 延时{current_delay}ms")
                self._send(build_set_flash(enable=True, delay_ms=current_delay))

                #等待一小段时间确保设置生效
                time.sleep(0.1)

                #发送拍照命令（测试模式，带延时参数）
                self._send(build_capture(test_mode=True, test_delay_ms=current_delay))

                #等待1秒（或停止信号）
                self._flash_test_stop_event.wait(1.0)

                #增加延时
                current_delay += step

        except Exception as e:
            logger.error(f"闪光灯测试异常: {e}")
        finally:
            #恢复UI状态
            self._is_flash_testing = False
            self.after(0, self._reset_flash_test_ui)
            logger.info(f"闪光灯测试结束, 共拍摄{current_count}张")

    def _update_flash_test_ui(self, current_delay: int, current_count: int, total_count: int):
        """更新闪光灯测试UI"""
        self.flash_test_current_label.config(text=f"{current_delay}ms")
        self.flash_test_progress_label.config(text=f"第{current_count}张/共{total_count}张")

    def _reset_flash_test_ui(self):
        """重置闪光灯测试UI"""
        self.flash_test_start_btn.config(state=tk.NORMAL)
        self.flash_test_stop_btn.config(state=tk.DISABLED)
        self.flash_test_status_label.config(text="等待开始", foreground="gray")
        self.flash_test_current_label.config(text="--")
        self.flash_test_progress_label.config(text="--")

    def set_enabled(self, enabled: bool):
        """
        设置控制面板启用/禁用状态

        Args:
            enabled: True启用，False禁用
        """
        state = tk.NORMAL if enabled else tk.DISABLED

        #分辨率
        self.param_res_combo.config(state="readonly" if enabled else tk.DISABLED)

        #拍照
        self.capture_btn.config(state=state)

        #连续拍照
        self.continuous_start_btn.config(state=state if not self._is_continuous else tk.DISABLED)
        self.continuous_stop_btn.config(state=state if self._is_continuous else tk.DISABLED)

        #录像
        self.record_duration_entry.config(state=state)
        self.record_res_combo.config(state="readonly" if enabled else tk.DISABLED)
        self.record_fps_combo.config(state="readonly" if enabled else tk.DISABLED)
        self.record_start_btn.config(state=state if not self._is_recording else tk.DISABLED)
        self.record_stop_btn.config(state=state if self._is_recording else tk.DISABLED)

        #预览
        self.preview_res_combo.config(state="readonly" if enabled else tk.DISABLED)
        self.preview_fps_combo.config(state="readonly" if enabled else tk.DISABLED)
        self.preview_start_btn.config(state=state if not self._is_previewing else tk.DISABLED)
        self.preview_stop_btn.config(state=state if self._is_previewing else tk.DISABLED)

        #参数
        self.exposure_mode_combo.config(state="readonly" if enabled else tk.DISABLED)
        if enabled and self.exposure_mode_var.get() == "手动":
            self.exposure_value_entry.config(state=tk.NORMAL)
        else:
            self.exposure_value_entry.config(state=tk.DISABLED)

        #自动增益
        self.gain_auto_check.config(state=state)
        if enabled and not self.gain_auto_var.get():
            self.gain_entry.config(state=tk.NORMAL)
        else:
            self.gain_entry.config(state=tk.DISABLED)

        self.wb_mode_combo.config(state="readonly" if enabled else tk.DISABLED)

        #帧率限制
        self.fps_limit_check.config(state=state)
        if enabled and self.fps_limit_var.get():
            self.fps_spinbox.config(state=tk.NORMAL)
        else:
            self.fps_spinbox.config(state=tk.DISABLED)

        #像素格式
        self.pixel_format_combo.config(state="readonly" if enabled else tk.DISABLED)

        self.apply_params_btn.config(state=state)

        #闪光灯
        self.flash_enable_check.config(state=state)
        self.flash_delay_entry.config(state=state)
        self.apply_flash_btn.config(state=state)

    def set_recording_state(self, is_recording: bool):
        """
        设置录像状态

        Args:
            is_recording: 是否正在录像
        """
        self._is_recording = is_recording

        if is_recording:
            self.record_status_label.config(text="录像中...", foreground="red")
            self.record_start_btn.config(state=tk.DISABLED)
            self.record_stop_btn.config(state=tk.NORMAL)
            #录像时禁用拍照
            self.capture_btn.config(state=tk.DISABLED)
            self.record_live_fps_label.config(text="0.0", foreground="green")
        else:
            self.record_status_label.config(text="未录像", foreground="gray")
            self.record_start_btn.config(state=tk.NORMAL)
            self.record_stop_btn.config(state=tk.DISABLED)
            self.capture_btn.config(state=tk.NORMAL)
            self.record_live_fps_label.config(text="--", foreground="gray")

    def set_preview_state(self, is_previewing: bool):
        """
        设置预览状态

        Args:
            is_previewing: 是否正在预览
        """
        self._is_previewing = is_previewing

        if is_previewing:
            self.preview_status_label.config(text="预览中...", foreground="green")
            self.preview_start_btn.config(state=tk.DISABLED)
            self.preview_stop_btn.config(state=tk.NORMAL)
        else:
            self.preview_status_label.config(text="未预览", foreground="gray")
            self.preview_start_btn.config(state=tk.NORMAL)
            self.preview_stop_btn.config(state=tk.DISABLED)

    def set_continuous_state(self, is_continuous: bool):
        """
        设置连续拍照状态

        Args:
            is_continuous: 是否正在连续拍照
        """
        self._is_continuous = is_continuous

        if is_continuous:
            self.continuous_status_label.config(text="连拍中...", foreground="orange")
            self.continuous_start_btn.config(state=tk.DISABLED)
            self.continuous_stop_btn.config(state=tk.NORMAL)
            #连拍时禁用单次拍照和录像
            self.capture_btn.config(state=tk.DISABLED)
            self.record_start_btn.config(state=tk.DISABLED)
        else:
            self.continuous_status_label.config(text="未连拍", foreground="gray")
            self.continuous_start_btn.config(state=tk.NORMAL)
            self.continuous_stop_btn.config(state=tk.DISABLED)
            #恢复单次拍照和录像按钮（如果不在录像中）
            if not self._is_recording:
                self.capture_btn.config(state=tk.NORMAL)
                self.record_start_btn.config(state=tk.NORMAL)

    def set_last_capture_file(self, filename: str):
        """
        设置最后拍照的文件名

        Args:
            filename: 文件名
        """
        self._last_capture_file = filename
        self.capture_file_label.config(text=filename, foreground="blue")

    def set_record_realtime_fps(self, fps: float):
        """
        更新录像实时帧率显示

        Args:
            fps: 实时帧率
        """
        if not self._is_recording:
            self.record_live_fps_label.config(text="--", foreground="gray")
            return

        fps = max(0.0, float(fps))
        self.record_live_fps_label.config(text=f"{fps:.1f}", foreground="green")

    def update_params(self, exposure_mode: int, exposure_value: int, gain: int, wb_mode: int,
                      width: Optional[int] = None, height: Optional[int] = None,
                      gain_auto: Optional[bool] = None, fps_limit: Optional[bool] = None,
                      fps: Optional[float] = None, pixel_format_index: Optional[int] = None):
        """
        更新参数显示

        Args:
            exposure_mode: 曝光模式（0-自动，1-手动）
            exposure_value: 曝光值（微秒）
            gain: 增益值
            wb_mode: 白平衡模式（0-自动，1-手动）
            width: 图像宽度
            height: 图像高度
            gain_auto: 自动增益是否开启（None表示不更新）
            fps_limit: 帧率限制是否开启（None表示不更新）
            fps: 帧率值（None表示不更新）
            pixel_format_index: 像素格式索引（None表示不更新）
        """
        if not self._widget_has_focus(self.exposure_value_entry):
            self.exposure_value_var.set(str(exposure_value))
        if not self._widget_has_focus(self.gain_entry):
            self.gain_var.set(str(gain))
        if not self._widget_has_focus(self.flash_delay_entry):
            # 闪光延时当前未由参数回包携带，这里仅避免编辑中被其他回写逻辑覆盖。
            pass

        if not self._widget_has_focus(self.exposure_mode_combo):
            self.exposure_mode_var.set("自动" if exposure_mode == 0 else "手动")
        if not self._widget_has_focus(self.wb_mode_combo):
            # 0-连续, 1-一次, 2-关闭
            wb_mode_map = {0: "连续", 1: "一次", 2: "关闭"}
            self.wb_mode_var.set(wb_mode_map.get(wb_mode, "关闭"))

        #更新自动增益
        if gain_auto is not None:
            self.gain_auto_var.set(gain_auto)
            self._on_gain_auto_changed()

        #更新帧率限制
        if fps_limit is not None:
            self.fps_limit_var.set(fps_limit)
            self._on_fps_limit_changed()
        if fps is not None and not self._widget_has_focus(self.fps_spinbox):
            self.fps_var.set(str(fps))

        #更新像素格式
        if pixel_format_index is not None and 0 <= pixel_format_index < len(PIXEL_FORMAT_OPTIONS):
            self.pixel_format_var.set(PIXEL_FORMAT_OPTIONS[pixel_format_index][0])

        #更新分辨率
        if width is not None and height is not None:
            self._set_param_resolution(width, height)

        #更新曝光输入框状态
        self._on_exposure_mode_changed()


if __name__ == '__main__':
    #测试代码
    import sys
    logger.remove()
    logger.add(sys.stdout, level="DEBUG")

    def mock_send(data: bytes) -> bool:
        print(f"发送数据: {data.hex().upper()}")
        return True

    root = tk.Tk()
    root.title("控制面板测试")
    root.geometry("300x700")

    panel = ControlPanel(root, mock_send)
    panel.pack(fill=tk.BOTH, expand=True)

    #测试状态设置
    root.after(2000, lambda: panel.set_recording_state(True))
    root.after(4000, lambda: panel.set_recording_state(False))
    root.after(3000, lambda: panel.set_preview_state(True))
    root.after(5000, lambda: panel.set_last_capture_file("IMG_20260121_120000.jpg"))

    root.mainloop()
