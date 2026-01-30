#-*- coding: utf-8 -*-
"""
控制面板模块

功能:
- 拍照控制（拍照按钮、显示最后拍照文件名）
- 录像控制（开始/停止、时长、分辨率、帧率、状态显示）
- 预览控制（开启/停止、分辨率、帧率、状态显示）
- 参数设置（曝光模式/值、增益、白平衡模式）
- 查询功能（状态、参数、分辨率列表）
"""

import tkinter as tk
from tkinter import ttk, messagebox
from typing import Callable, Optional, Tuple
from loguru import logger

from protocol_builder import (
    build_capture, build_record_start, build_record_stop,
    build_preview_start, build_preview_stop,
    build_continuous_start, build_continuous_stop,
    build_set_exposure, build_set_gain, build_set_white_balance,
    build_set_resolution,
    build_set_gain_auto, build_set_frame_rate, build_set_pixel_format,
    build_query_status, build_query_params, build_query_resolutions
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

#帧率选项
RECORD_FPS_OPTIONS = list(range(1, 31))  #1-30
PREVIEW_FPS_OPTIONS = list(range(5, 31))  #5-30

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

        #查询功能
        self._create_query_section()

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
            values=RECORD_FPS_OPTIONS,
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
            values=PREVIEW_FPS_OPTIONS,
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
        self.wb_mode_var = tk.StringVar(value="自动")
        self.wb_mode_combo = ttk.Combobox(
            wb_frame,
            textvariable=self.wb_mode_var,
            values=["自动", "手动"],
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
            from_=1,
            to=30,
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

    def _create_query_section(self):
        """创建查询功能区域"""
        frame = ttk.LabelFrame(self, text="查询功能", padding="5")
        frame.pack(fill=tk.X, pady=(0, 5))

        #查询状态
        self.query_status_btn = ttk.Button(frame, text="查询状态", command=self._on_query_status)
        self.query_status_btn.pack(fill=tk.X, pady=2)

        #查询参数
        self.query_params_btn = ttk.Button(frame, text="查询参数", command=self._on_query_params)
        self.query_params_btn.pack(fill=tk.X, pady=2)

        #查询分辨率列表
        self.query_res_btn = ttk.Button(frame, text="查询分辨率列表", command=self._on_query_resolutions)
        self.query_res_btn.pack(fill=tk.X, pady=2)

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
            fps = max(1, min(30, fps))
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
            fps = max(5, min(30, fps))
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
        wb_mode = 0 if self.wb_mode_var.get() == "自动" else 1
        logger.info(f"发送白平衡设置: mode={wb_mode}")
        self._send(build_set_white_balance(mode=wb_mode))

        #帧率设置
        fps_enable = self.fps_limit_var.get()
        fps_str = self.fps_var.get()
        try:
            fps_value = float(fps_str)
            fps_value = max(1.0, min(30.0, fps_value))
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

    def _on_query_status(self):
        """查询状态按钮点击"""
        logger.info("发送查询状态命令")
        self._send(build_query_status())

    def _on_query_params(self):
        """查询参数按钮点击"""
        logger.info("发送查询参数命令")
        self._send(build_query_params())

    def _on_query_resolutions(self):
        """查询分辨率列表按钮点击"""
        logger.info("发送查询分辨率列表命令")
        self._send(build_query_resolutions())

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

        #查询
        self.query_status_btn.config(state=state)
        self.query_params_btn.config(state=state)
        self.query_res_btn.config(state=state)

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
        else:
            self.record_status_label.config(text="未录像", foreground="gray")
            self.record_start_btn.config(state=tk.NORMAL)
            self.record_stop_btn.config(state=tk.DISABLED)
            self.capture_btn.config(state=tk.NORMAL)

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
        self.exposure_mode_var.set("自动" if exposure_mode == 0 else "手动")
        self.exposure_value_var.set(str(exposure_value))
        self.gain_var.set(str(gain))
        self.wb_mode_var.set("自动" if wb_mode == 0 else "手动")

        #更新自动增益
        if gain_auto is not None:
            self.gain_auto_var.set(gain_auto)
            self._on_gain_auto_changed()

        #更新帧率限制
        if fps_limit is not None:
            self.fps_limit_var.set(fps_limit)
            self._on_fps_limit_changed()
        if fps is not None:
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
