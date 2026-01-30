#-*- coding: utf-8 -*-
"""
主窗口模块

功能:
- 主窗口布局
- 连接状态指示
- 服务器地址/端口输入
- 连接/断开按钮
- 预留控制面板区域
- 预留预览显示区域
- 预留状态监控区域
- 心跳发送（每5秒）

异常处理:
- 连接异常处理和用户提示
- 命令发送失败处理
- 预览帧解析异常处理
- UI线程安全更新
"""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
from typing import Optional
from loguru import logger

from tcp_client import TcpClient, ConnectionState
from protocol_builder import (
    Command, parse_ack_success, parse_ack_failed,
    build_capture, build_preview_start, build_preview_stop,
    build_record_start, build_record_stop,
    build_query_status, build_query_params
)
from control_panel import ControlPanel
from status_monitor import StatusMonitor
from preview_widget import PreviewWidget
from settings_dialog import SettingsDialog


#错误码描述映射（与客户端保持一致）
ERROR_DESCRIPTIONS = {
    0x0101: "相机未连接",
    0x0102: "相机初始化失败",
    0x0103: "图像采集超时",
    0x0104: "参数设置失败",
    0x0105: "相机断线",
    0x0106: "不支持的分辨率",
    0x0201: "磁盘空间不足",
    0x0202: "写入权限不足",
    0x0203: "文件创建失败",
    0x0301: "正在录像中",
    0x0302: "正在拍照中",
    0x0303: "预览未开启",
    0x0304: "预览已开启",
    0x0401: "XOR校验失败",
    0x0402: "帧格式错误",
    0x0403: "未知命令码",
    0x0404: "数据段长度错误",
    0x0405: "协议版本不兼容",
    0x0501: "JPEG编码失败",
    0x0502: "H.264编码失败",
    0x0503: "视频编码器初始化失败",
    0xFFFF: "未知错误",
}


def get_error_description(code: int) -> str:
    """获取错误码描述"""
    return ERROR_DESCRIPTIONS.get(code, f"未知错误码: 0x{code:04X}")


class MainWindow:
    """
    主窗口类

    异常处理策略:
    - 连接异常: 显示错误对话框，更新状态指示
    - 命令发送失败: 显示状态栏提示，记录日志
    - 预览帧异常: 静默处理，记录日志
    - UI更新异常: 使用after()确保线程安全
    """

    def __init__(self):
        #创建主窗口
        self.root = tk.Tk()
        self.root.title("Basler 相机控制系统")
        self.root.geometry("1200x800")
        self.root.minsize(800, 600)

        #TCP客户端
        self.client = TcpClient()
        self.client.set_on_state_changed(self._on_connection_state_changed)
        self.client.set_on_data_received(self._on_data_received)
        self.client.set_on_error(self._on_error)
        self.client.set_on_reconnect_failed(self._on_reconnect_failed)

        #启用自动重连（间隔5秒，最多10次）
        self.client.set_reconnect(True, interval=5.0, max_attempts=10)

        #状态变量
        self._connection_state = ConnectionState.DISCONNECTED
        self._last_error_code: Optional[int] = None

        #加载配置
        self._settings = SettingsDialog.get_settings()

        #创建界面
        self._create_menu()
        self._create_ui()

        #应用配置到界面
        self._apply_settings()

        #绑定关闭事件
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _create_menu(self):
        """创建菜单栏"""
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)

        #文件菜单
        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="文件", menu=file_menu)
        file_menu.add_command(label="设置...", command=self._on_settings_click)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self._on_close)

        #帮助菜单
        help_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="帮助", menu=help_menu)
        help_menu.add_command(label="关于", command=self._on_about_click)

    def _apply_settings(self):
        """应用配置到界面"""
        #连接设置
        conn = self._settings.get("connection", {})
        self.host_entry.delete(0, tk.END)
        self.host_entry.insert(0, conn.get("host", "127.0.0.1"))
        self.port_entry.delete(0, tk.END)
        self.port_entry.insert(0, str(conn.get("port", 8899)))

        #更新自动重连设置
        auto_reconnect = conn.get("auto_reconnect", True)
        reconnect_interval = conn.get("reconnect_interval", 5)
        self.client.set_reconnect(auto_reconnect, interval=float(reconnect_interval), max_attempts=10)

        logger.info("配置已应用到界面")

    def _on_settings_click(self):
        """设置菜单点击"""
        dialog = SettingsDialog(self.root, on_save=self._on_settings_saved)
        dialog.show()

    def _on_settings_saved(self, settings: dict):
        """配置保存回调"""
        self._settings = settings
        self._apply_settings()
        self._log("配置已更新")

    def _on_about_click(self):
        """关于菜单点击"""
        messagebox.showinfo(
            "关于",
            "Basler 相机控制系统\n\n"
            "版本: 1.0.0\n"
            "协议版本: 2.0\n\n"
            "基于 Python + Tkinter 开发"
        )

    def _create_ui(self):
        """创建用户界面"""
        #主框架
        main_frame = ttk.Frame(self.root, padding="5")
        main_frame.pack(fill=tk.BOTH, expand=True)

        #顶部连接区域
        self._create_connection_frame(main_frame)

        #中间区域（左右分栏）
        middle_frame = ttk.Frame(main_frame)
        middle_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        #左侧控制面板
        self._create_control_panel(middle_frame)

        #右侧预览区域
        self._create_preview_area(middle_frame)

        #底部状态监控区域
        self._create_status_area(main_frame)

    def _create_connection_frame(self, parent):
        """创建连接控制区域"""
        conn_frame = ttk.LabelFrame(parent, text="服务器连接", padding="5")
        conn_frame.pack(fill=tk.X, pady=(0, 5))

        #服务器地址
        ttk.Label(conn_frame, text="服务器地址:").pack(side=tk.LEFT, padx=(0, 5))
        self.host_entry = ttk.Entry(conn_frame, width=15)
        self.host_entry.insert(0, "127.0.0.1")
        self.host_entry.pack(side=tk.LEFT, padx=(0, 10))

        #端口
        ttk.Label(conn_frame, text="端口:").pack(side=tk.LEFT, padx=(0, 5))
        self.port_entry = ttk.Entry(conn_frame, width=6)
        self.port_entry.insert(0, "8899")
        self.port_entry.pack(side=tk.LEFT, padx=(0, 10))

        #连接按钮
        self.connect_btn = ttk.Button(conn_frame, text="连接", command=self._on_connect_click)
        self.connect_btn.pack(side=tk.LEFT, padx=(0, 5))

        #断开按钮
        self.disconnect_btn = ttk.Button(conn_frame, text="断开", command=self._on_disconnect_click, state=tk.DISABLED)
        self.disconnect_btn.pack(side=tk.LEFT, padx=(0, 10))

        #连接状态指示
        ttk.Label(conn_frame, text="状态:").pack(side=tk.LEFT, padx=(10, 5))
        self.status_label = ttk.Label(conn_frame, text="未连接", foreground="gray")
        self.status_label.pack(side=tk.LEFT)

        #状态指示灯
        self.status_indicator = tk.Canvas(conn_frame, width=16, height=16, highlightthickness=0)
        self.status_indicator.pack(side=tk.LEFT, padx=5)
        self._update_status_indicator("gray")

    def _create_control_panel(self, parent):
        """创建控制面板区域"""
        control_frame = ttk.Frame(parent)
        control_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 5))

        self.control_panel = ControlPanel(control_frame, self._send_command)
        self.control_panel.pack(fill=tk.Y, expand=True)
        self.control_panel.set_enabled(False)

    def _create_preview_area(self, parent):
        """创建预览显示区域"""
        preview_frame = ttk.LabelFrame(parent, text="实时预览", padding="5")
        preview_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        #使用PreviewWidget组件
        self.preview_widget = PreviewWidget(preview_frame)
        self.preview_widget.pack(fill=tk.BOTH, expand=True)

    def _create_status_area(self, parent):
        """创建状态监控区域"""
        status_frame = ttk.LabelFrame(parent, text="状态监控", padding="5")
        status_frame.pack(fill=tk.BOTH, expand=True, pady=(5, 0))

        self.status_monitor = StatusMonitor(status_frame)
        self.status_monitor.pack(fill=tk.BOTH, expand=True)

    def _update_status_indicator(self, color: str):
        """更新状态指示灯"""
        self.status_indicator.delete("all")
        self.status_indicator.create_oval(2, 2, 14, 14, fill=color, outline="")

    def _log(self, message: str):
        """添加日志"""
        if hasattr(self, 'status_monitor'):
            self.status_monitor.log_info(message)
        else:
            logger.info(message)

    def _log_with_level(self, message: str, level: str):
        if hasattr(self, 'status_monitor'):
            log_method = getattr(self.status_monitor, f"log_{level}", self.status_monitor.log_info)
            log_method(message)
        else:
            logger.info(message)

    def _update_ui_state(self):
        """根据连接状态更新UI"""
        connected = self._connection_state == ConnectionState.CONNECTED
        reconnecting = self._connection_state == ConnectionState.RECONNECTING

        #连接按钮
        self.connect_btn.config(state=tk.NORMAL if not connected and not reconnecting else tk.DISABLED)
        self.disconnect_btn.config(state=tk.NORMAL if connected or reconnecting else tk.DISABLED)
        self.host_entry.config(state=tk.NORMAL if not connected and not reconnecting else tk.DISABLED)
        self.port_entry.config(state=tk.NORMAL if not connected and not reconnecting else tk.DISABLED)

        #控制按钮
        if hasattr(self, 'control_panel'):
            self.control_panel.set_enabled(connected)

        #状态显示
        if self._connection_state == ConnectionState.DISCONNECTED:
            self.status_label.config(text="未连接", foreground="gray")
            self._update_status_indicator("gray")
            if hasattr(self, 'status_monitor'):
                self.status_monitor.reset()
        elif self._connection_state == ConnectionState.CONNECTING:
            self.status_label.config(text="连接中...", foreground="orange")
            self._update_status_indicator("orange")
        elif self._connection_state == ConnectionState.RECONNECTING:
            self.status_label.config(text="重连中...", foreground="orange")
            self._update_status_indicator("orange")
        else:
            self.status_label.config(text="已连接", foreground="green")
            self._update_status_indicator("green")

    def _on_connection_state_changed(self, state: int):
        """连接状态变化回调（在工作线程中调用）"""
        self._connection_state = state
        #在主线程中更新UI
        self.root.after(0, self._update_ui_state)

        if state == ConnectionState.CONNECTED:
            self.root.after(0, lambda: self._log("已连接到服务器"))
        elif state == ConnectionState.DISCONNECTED:
            self.root.after(0, lambda: self._log("已断开连接"))
        elif state == ConnectionState.RECONNECTING:
            self.root.after(0, lambda: self._log("正在尝试重连..."))

    def _on_reconnect_failed(self):
        """重连失败回调（在工作线程中调用）"""
        def show_reconnect_failed():
            self._log("重连失败，已达到最大重试次数")
            messagebox.showwarning(
                "连接失败",
                "无法连接到服务器，已达到最大重试次数。\n请检查服务器是否运行，然后手动重新连接。"
            )
        self.root.after(0, show_reconnect_failed)

    def _on_data_received(self, version: int, cmd: int, data: bytes):
        """数据接收回调（在工作线程中调用）"""
        #在主线程中处理
        self.root.after(0, lambda: self._handle_response(version, cmd, data))

    def _handle_response(self, version: int, cmd: int, data: bytes):
        """处理服务器响应"""
        try:
            if cmd == Command.ACK_SUCCESS:
                orig_cmd = parse_ack_success(data)
                if orig_cmd is not None:
                    self._log_with_level(f"操作成功: 命令0x{orig_cmd:02X}", "success")

            elif cmd == Command.ACK_FAILED:
                result = parse_ack_failed(data)
                if result:
                    orig_cmd, error_code = result
                    self._last_error_code = error_code
                    error_desc = get_error_description(error_code)
                    self._log_with_level(
                        f"操作失败: 命令0x{orig_cmd:02X}, {error_desc} (0x{error_code:04X})",
                        "error"
                    )

            elif cmd == Command.STATUS_REPORT:
                if len(data) > 0 and hasattr(self, 'status_monitor'):
                    status_byte = data[0]
                    status = self.status_monitor.parse_status_byte(status_byte)
                    self.status_monitor.update_status(status)
                    if hasattr(self, 'control_panel'):
                        self.control_panel.set_recording_state(status.get('recording', False))
                        self.control_panel.set_preview_state(status.get('previewing', False))
                        self.control_panel.set_continuous_state(status.get('continuous', False))
                        if not status.get('previewing', False) and hasattr(self, 'preview_widget'):
                            self.preview_widget.clear()

            elif cmd == Command.PARAMS_REPORT:
                if hasattr(self, 'status_monitor'):
                    params = self.status_monitor.parse_params(data)
                    if params:
                        self.status_monitor.update_params(params)
                        if hasattr(self, 'control_panel'):
                            self.control_panel.update_params(
                                exposure_mode=params.get('exposure_mode', 0),
                                exposure_value=params.get('exposure_value', 0),
                                gain=params.get('gain', 0),
                                wb_mode=params.get('wb_mode', 0),
                                width=params.get('width'),
                                height=params.get('height')
                            )

            elif cmd == Command.CAPTURE_DONE:
                if len(data) > 0:
                    filename_len = data[0]
                    filename = data[1:1+filename_len].decode('utf-8', errors='ignore')
                    if hasattr(self, 'control_panel'):
                        self.control_panel.set_last_capture_file(filename)
                    self._log_with_level(f"拍照完成: {filename}", "success")
                else:
                    self._log_with_level("拍照完成", "success")

            elif cmd == Command.RECORD_DONE:
                if len(data) > 0:
                    filename_len = data[0]
                    filename = data[1:1+filename_len].decode('utf-8', errors='ignore')
                    self._log_with_level(f"录像完成: {filename}", "success")
                else:
                    self._log_with_level("录像完成", "success")

            elif cmd == Command.PREVIEW_FRAME:
                #预览帧处理
                self._handle_preview_frame(data)

            elif cmd == Command.HEARTBEAT:
                #心跳响应
                logger.debug("收到心跳响应")

            else:
                self._log(f"收到未知命令: 0x{cmd:02X}")

        except Exception as e:
            logger.error(f"处理响应异常: {e}")
            self._log(f"处理响应异常: {e}")

    def _on_error(self, message: str):
        """错误回调（在工作线程中调用）"""
        self.root.after(0, lambda: self._log_with_level(f"错误: {message}", "error"))

    def _handle_preview_frame(self, data: bytes):
        """
        处理预览帧数据

        Args:
            data: 0xC0预览帧数据段
        """
        try:
            if hasattr(self, 'preview_widget'):
                success = self.preview_widget.update_frame_from_protocol(data)
                if not success:
                    logger.warning("预览帧处理失败")
        except Exception as e:
            logger.error(f"预览帧处理异常: {e}")

    def _send_command(self, data: bytes) -> bool:
        """发送协议帧"""
        if not self.client.is_connected:
            self._log_with_level("未连接服务器，无法发送命令", "warning")
            return False
        return self.client.send(data)

    def _on_connect_click(self):
        """连接按钮点击"""
        host = self.host_entry.get().strip()
        port_str = self.port_entry.get().strip()

        if not host:
            messagebox.showerror("错误", "请输入服务器地址")
            return

        try:
            port = int(port_str)
            if port < 1 or port > 65535:
                raise ValueError()
        except ValueError:
            messagebox.showerror("错误", "端口号无效（1-65535）")
            return

        self._log(f"正在连接 {host}:{port}...")

        #在后台线程中连接
        def connect_task():
            success = self.client.connect(host, port, timeout=5.0)
            if not success:
                self.root.after(0, lambda: self._log("连接失败"))

        threading.Thread(target=connect_task, daemon=True).start()

    def _on_disconnect_click(self):
        """断开按钮点击"""
        self._log("正在断开连接...")
        self.client.disconnect()

    def _on_capture_click(self):
        """拍照按钮点击"""
        self._log("发送拍照命令...")
        if not self.client.send(build_capture()):
            self._log("发送拍照命令失败")

    def _on_preview_start_click(self):
        """开启预览按钮点击"""
        self._log("发送开启预览命令...")
        if not self.client.send(build_preview_start(resolution_index=0, fps=10)):
            self._log("发送开启预览命令失败")

    def _on_preview_stop_click(self):
        """停止预览按钮点击"""
        self._log("发送停止预览命令...")
        if not self.client.send(build_preview_stop()):
            self._log("发送停止预览命令失败")
        #清除预览显示
        if hasattr(self, 'preview_widget'):
            self.preview_widget.clear()

    def _on_record_start_click(self):
        """开始录像按钮点击"""
        self._log("发送开始录像命令...")
        if not self.client.send(build_record_start(duration=0, resolution_index=0, fps=5)):
            self._log("发送开始录像命令失败")

    def _on_record_stop_click(self):
        """停止录像按钮点击"""
        self._log("发送停止录像命令...")
        if not self.client.send(build_record_stop()):
            self._log("发送停止录像命令失败")

    def _on_query_status_click(self):
        """查询状态按钮点击"""
        self._log("发送查询状态命令...")
        if not self.client.send(build_query_status()):
            self._log("发送查询状态命令失败")

    def _on_query_params_click(self):
        """查询参数按钮点击"""
        self._log("发送查询参数命令...")
        if not self.client.send(build_query_params()):
            self._log("发送查询参数命令失败")

    def _on_close(self):
        """窗口关闭事件"""
        if self.client.is_connected:
            self.client.disconnect()
        self.root.destroy()

    def run(self):
        """运行主窗口"""
        logger.info("启动主窗口")
        self.root.mainloop()


if __name__ == '__main__':
    #测试代码
    import sys
    logger.remove()
    logger.add(sys.stdout, level="DEBUG")

    window = MainWindow()
    window.run()
