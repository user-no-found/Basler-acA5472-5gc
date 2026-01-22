#-*- coding: utf-8 -*-
"""
状态监控模块

功能:
- 相机状态显示（连接/拍照/录像/预览）
- 参数回显（曝光/增益/白平衡/分辨率）
- 操作日志记录（时间戳+内容，支持滚动）
- 错误信息展示（错误码+描述，高亮显示）
"""

import tkinter as tk
from tkinter import ttk
from datetime import datetime
from typing import Optional, Dict, Any, Callable
from loguru import logger

from error_codes import get_error_message, get_error_category, is_success


class StatusMonitor(ttk.Frame):
    """状态监控组件"""

    #最大日志条数
    MAX_LOG_ENTRIES = 500

    def __init__(self, parent, **kwargs):
        """
        初始化状态监控组件

        Args:
            parent: 父容器
        """
        super().__init__(parent, **kwargs)

        #状态数据
        self._camera_status = {
            'camera_connected': False,
            'capturing': False,
            'recording': False,
            'previewing': False,
        }

        #参数数据
        self._camera_params = {
            'exposure_mode': 0,
            'exposure_value': 0,
            'gain': 0,
            'wb_mode': 0,
            'wb_r': 0,
            'wb_g': 0,
            'wb_b': 0,
            'width': 0,
            'height': 0,
        }

        #日志计数
        self._log_count = 0

        #创建界面
        self._create_ui()

    def _create_ui(self):
        """创建用户界面"""
        #主布局：左侧状态+参数，右侧日志
        self.columnconfigure(0, weight=0)  #状态区域固定宽度
        self.columnconfigure(1, weight=1)  #日志区域自适应
        self.rowconfigure(0, weight=1)

        #左侧面板（状态+参数）
        left_panel = ttk.Frame(self)
        left_panel.grid(row=0, column=0, sticky='ns', padx=(0, 5))

        #状态显示区域
        self._create_status_frame(left_panel)

        #参数回显区域
        self._create_params_frame(left_panel)

        #右侧日志区域
        self._create_log_frame()

    def _create_status_frame(self, parent):
        """创建状态显示区域"""
        status_frame = ttk.LabelFrame(parent, text="设备状态", padding="5")
        status_frame.pack(fill=tk.X, pady=(0, 5))

        #状态指示器样式
        self._status_labels = {}
        self._status_indicators = {}

        status_items = [
            ('camera_connected', '相机连接'),
            ('capturing', '拍照状态'),
            ('recording', '录像状态'),
            ('previewing', '预览状态'),
        ]

        for key, label_text in status_items:
            row_frame = ttk.Frame(status_frame)
            row_frame.pack(fill=tk.X, pady=2)

            #标签
            ttk.Label(row_frame, text=f"{label_text}:", width=10).pack(side=tk.LEFT)

            #状态指示灯
            indicator = tk.Canvas(row_frame, width=12, height=12, highlightthickness=0)
            indicator.pack(side=tk.LEFT, padx=(0, 5))
            indicator.create_oval(1, 1, 11, 11, fill='gray', outline='', tags='indicator')
            self._status_indicators[key] = indicator

            #状态文字
            status_label = ttk.Label(row_frame, text="--", width=8)
            status_label.pack(side=tk.LEFT)
            self._status_labels[key] = status_label

    def _create_params_frame(self, parent):
        """创建参数回显区域"""
        params_frame = ttk.LabelFrame(parent, text="相机参数", padding="5")
        params_frame.pack(fill=tk.X, pady=(0, 5))

        #参数标签
        self._param_labels = {}

        param_items = [
            ('exposure', '曝光'),
            ('gain', '增益'),
            ('white_balance', '白平衡'),
            ('resolution', '分辨率'),
        ]

        for key, label_text in param_items:
            row_frame = ttk.Frame(params_frame)
            row_frame.pack(fill=tk.X, pady=2)

            ttk.Label(row_frame, text=f"{label_text}:", width=8).pack(side=tk.LEFT)

            value_label = ttk.Label(row_frame, text="--", anchor=tk.W)
            value_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self._param_labels[key] = value_label

    def _create_log_frame(self):
        """创建日志显示区域"""
        log_frame = ttk.LabelFrame(self, text="操作日志", padding="5")
        log_frame.grid(row=0, column=1, sticky='nsew')
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        #日志文本框
        self._log_text = tk.Text(
            log_frame,
            height=8,
            state=tk.DISABLED,
            wrap=tk.WORD,
            font=('Consolas', 9)
        )
        self._log_text.grid(row=0, column=0, sticky='nsew')

        #滚动条
        scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self._log_text.yview)
        scrollbar.grid(row=0, column=1, sticky='ns')
        self._log_text.config(yscrollcommand=scrollbar.set)

        #配置标签样式
        self._log_text.tag_configure('timestamp', foreground='#666666')
        self._log_text.tag_configure('info', foreground='#000000')
        self._log_text.tag_configure('success', foreground='#008000')
        self._log_text.tag_configure('warning', foreground='#FF8C00')
        self._log_text.tag_configure('error', foreground='#FF0000', font=('Consolas', 9, 'bold'))

        #清空按钮
        btn_frame = ttk.Frame(log_frame)
        btn_frame.grid(row=1, column=0, columnspan=2, sticky='e', pady=(5, 0))

        ttk.Button(btn_frame, text="清空日志", command=self.clear_log).pack(side=tk.RIGHT)

    def parse_status_byte(self, status: int) -> Dict[str, bool]:
        """
        解析状态字节（0xA0响应）

        Args:
            status: 状态字节

        Returns:
            状态字典
        """
        return {
            'camera_connected': bool(status & 0x01),
            'capturing': bool(status & 0x02),
            'recording': bool(status & 0x04),
            'previewing': bool(status & 0x08),
        }

    def parse_params(self, data: bytes) -> Optional[Dict[str, Any]]:
        """
        解析参数结构体（0xA1响应）

        Args:
            data: 18字节参数数据

        Returns:
            参数字典，解析失败返回None
        """
        if len(data) < 18:
            logger.warning(f"参数数据长度不足: {len(data)} < 18")
            return None

        try:
            return {
                'exposure_mode': data[0],  #0-自动, 1-手动
                'exposure_value': int.from_bytes(data[1:5], 'big'),  #微秒
                'gain': int.from_bytes(data[5:7], 'big'),
                'wb_mode': data[7],  #0-自动, 1-手动
                'wb_r': int.from_bytes(data[8:10], 'big'),
                'wb_g': int.from_bytes(data[10:12], 'big'),
                'wb_b': int.from_bytes(data[12:14], 'big'),
                'width': int.from_bytes(data[14:16], 'big'),
                'height': int.from_bytes(data[16:18], 'big'),
            }
        except Exception as e:
            logger.error(f"参数解析失败: {e}")
            return None

    def update_status(self, status_data: Dict[str, bool]):
        """
        更新状态显示

        Args:
            status_data: 状态字典
        """
        self._camera_status.update(status_data)

        #更新UI
        status_text_map = {
            'camera_connected': ('已连接', '未连接'),
            'capturing': ('拍照中', '空闲'),
            'recording': ('录像中', '空闲'),
            'previewing': ('开启', '关闭'),
        }

        for key, (on_text, off_text) in status_text_map.items():
            if key in status_data:
                is_active = status_data[key]
                #更新指示灯颜色
                color = '#00CC00' if is_active else 'gray'
                self._status_indicators[key].itemconfig('indicator', fill=color)
                #更新文字
                text = on_text if is_active else off_text
                self._status_labels[key].config(text=text)

    def update_status_from_byte(self, status_byte: int):
        """
        从状态字节更新状态显示

        Args:
            status_byte: 状态字节
        """
        status_data = self.parse_status_byte(status_byte)
        self.update_status(status_data)

    def update_params(self, params_data: Dict[str, Any]):
        """
        更新参数回显

        Args:
            params_data: 参数字典
        """
        self._camera_params.update(params_data)

        #曝光显示
        exposure_mode = '自动' if params_data.get('exposure_mode', 0) == 0 else '手动'
        exposure_value = params_data.get('exposure_value', 0)
        self._param_labels['exposure'].config(
            text=f"{exposure_mode} / {exposure_value} us"
        )

        #增益显示
        gain = params_data.get('gain', 0)
        self._param_labels['gain'].config(text=f"{gain}")

        #白平衡显示
        wb_mode = '自动' if params_data.get('wb_mode', 0) == 0 else '手动'
        wb_r = params_data.get('wb_r', 0)
        wb_g = params_data.get('wb_g', 0)
        wb_b = params_data.get('wb_b', 0)
        self._param_labels['white_balance'].config(
            text=f"{wb_mode} / R:{wb_r} G:{wb_g} B:{wb_b}"
        )

        #分辨率显示
        width = params_data.get('width', 0)
        height = params_data.get('height', 0)
        self._param_labels['resolution'].config(text=f"{width} x {height}")

    def update_params_from_bytes(self, data: bytes):
        """
        从字节数据更新参数回显

        Args:
            data: 18字节参数数据
        """
        params = self.parse_params(data)
        if params:
            self.update_params(params)

    def log(self, message: str, level: str = 'info'):
        """
        添加日志记录

        Args:
            message: 日志内容
            level: 日志级别（info/success/warning/error）
        """
        #获取时间戳
        timestamp = datetime.now().strftime('%H:%M:%S')

        #启用编辑
        self._log_text.config(state=tk.NORMAL)

        #检查是否需要清理旧日志
        self._log_count += 1
        if self._log_count > self.MAX_LOG_ENTRIES:
            #删除前100行
            self._log_text.delete('1.0', '101.0')
            self._log_count -= 100

        #插入时间戳
        self._log_text.insert(tk.END, f"[{timestamp}] ", 'timestamp')

        #插入消息
        self._log_text.insert(tk.END, f"{message}\n", level)

        #滚动到底部
        self._log_text.see(tk.END)

        #禁用编辑
        self._log_text.config(state=tk.DISABLED)

        #同时输出到loguru
        log_func = getattr(logger, level if level != 'success' else 'info', logger.info)
        log_func(message)

    def log_info(self, message: str):
        """记录普通信息"""
        self.log(message, 'info')

    def log_success(self, message: str):
        """记录成功信息"""
        self.log(message, 'success')

    def log_warning(self, message: str):
        """记录警告信息"""
        self.log(message, 'warning')

    def log_error(self, message: str):
        """记录错误信息"""
        self.log(message, 'error')

    def log_error_code(self, error_code: int, context: str = ""):
        """
        记录错误码信息

        Args:
            error_code: 错误码
            context: 上下文信息（如命令名称）
        """
        error_msg = get_error_message(error_code)
        error_cat = get_error_category(error_code)

        if context:
            full_msg = f"{context} - [{error_cat}] 0x{error_code:04X}: {error_msg}"
        else:
            full_msg = f"[{error_cat}] 0x{error_code:04X}: {error_msg}"

        if is_success(error_code):
            self.log_success(full_msg)
        else:
            self.log_error(full_msg)

    def clear_log(self):
        """清空日志"""
        self._log_text.config(state=tk.NORMAL)
        self._log_text.delete('1.0', tk.END)
        self._log_text.config(state=tk.DISABLED)
        self._log_count = 0

    def get_status(self) -> Dict[str, bool]:
        """获取当前状态"""
        return self._camera_status.copy()

    def get_params(self) -> Dict[str, Any]:
        """获取当前参数"""
        return self._camera_params.copy()

    def reset(self):
        """重置所有状态和参数"""
        #重置状态
        self.update_status({
            'camera_connected': False,
            'capturing': False,
            'recording': False,
            'previewing': False,
        })

        #重置参数显示
        for label in self._param_labels.values():
            label.config(text="--")

        #重置参数数据
        self._camera_params = {
            'exposure_mode': 0,
            'exposure_value': 0,
            'gain': 0,
            'wb_mode': 0,
            'wb_r': 0,
            'wb_g': 0,
            'wb_b': 0,
            'width': 0,
            'height': 0,
        }


#测试代码
if __name__ == '__main__':
    import sys
    logger.remove()
    logger.add(sys.stdout, level="DEBUG")

    #创建测试窗口
    root = tk.Tk()
    root.title("状态监控测试")
    root.geometry("800x400")

    #创建状态监控组件
    monitor = StatusMonitor(root, padding="10")
    monitor.pack(fill=tk.BOTH, expand=True)

    #测试按钮
    btn_frame = ttk.Frame(root, padding="5")
    btn_frame.pack(fill=tk.X)

    def test_status():
        """测试状态更新"""
        monitor.update_status_from_byte(0x0F)  #全部激活
        monitor.log_info("状态更新测试")

    def test_params():
        """测试参数更新"""
        #构造测试数据（18字节）
        test_data = bytes([
            0x01,  #曝光模式：手动
            0x00, 0x00, 0x27, 0x10,  #曝光值：10000us
            0x00, 0x64,  #增益：100
            0x00,  #白平衡模式：自动
            0x00, 0x80,  #WB_R: 128
            0x00, 0x80,  #WB_G: 128
            0x00, 0x80,  #WB_B: 128
            0x07, 0x80,  #宽度：1920
            0x04, 0x38,  #高度：1080
        ])
        monitor.update_params_from_bytes(test_data)
        monitor.log_success("参数更新测试")

    def test_error():
        """测试错误显示"""
        monitor.log_error_code(0x0101, "拍照命令")
        monitor.log_error_code(0x0301, "录像命令")
        monitor.log_error_code(0x0000, "查询状态")

    def test_reset():
        """测试重置"""
        monitor.reset()
        monitor.log_warning("已重置状态")

    ttk.Button(btn_frame, text="测试状态", command=test_status).pack(side=tk.LEFT, padx=2)
    ttk.Button(btn_frame, text="测试参数", command=test_params).pack(side=tk.LEFT, padx=2)
    ttk.Button(btn_frame, text="测试错误", command=test_error).pack(side=tk.LEFT, padx=2)
    ttk.Button(btn_frame, text="重置", command=test_reset).pack(side=tk.LEFT, padx=2)

    root.mainloop()
