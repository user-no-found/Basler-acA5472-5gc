#-*- coding: utf-8 -*-
"""
配置对话框模块

功能:
- 连接设置（服务器地址、端口、超时、自动重连）
- 存储设置（图片/视频保存路径、JPEG质量）
- 预览设置（分辨率、帧率、JPEG质量）
- 日志设置（日志级别、保存路径）
- 配置持久化到 gui/config/settings.json
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import json
import os
from typing import Any, Dict, Optional, Callable
from pathlib import Path
from loguru import logger


class SettingsDialog:
    """配置对话框"""

    #默认配置
    DEFAULT_SETTINGS = {
        "connection": {
            "host": "127.0.0.1",
            "port": 8899,
            "timeout": 5,
            "auto_reconnect": True,
            "reconnect_interval": 5
        },
        "storage": {
            "image_path": "./images",
            "video_path": "./videos",
            "jpeg_quality": 95
        },
        "preview": {
            "resolution_index": 0,
            "fps": 10,
            "jpeg_quality": 80
        },
        "log": {
            "level": "INFO",
            "path": "./logs",
            "max_size_mb": 10,
            "backup_count": 10
        }
    }

    #支持的分辨率列表
    RESOLUTIONS = [
        "1920x1080",
        "1280x720",
        "640x480"
    ]

    #日志级别列表
    LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]

    def __init__(self, parent: tk.Tk, on_save: Optional[Callable[[Dict], None]] = None):
        """
        初始化配置对话框

        Args:
            parent: 父窗口
            on_save: 保存回调函数，接收配置字典
        """
        self.parent = parent
        self.on_save = on_save
        self.dialog: Optional[tk.Toplevel] = None
        self.result: Optional[Dict] = None

        #配置文件路径
        self._config_path = self._get_config_path()

        #加载当前配置
        self._settings = self._load_settings()

        #控件变量
        self._vars: Dict[str, tk.Variable] = {}

    def _get_config_path(self) -> str:
        """获取配置文件路径"""
        #gui/config/settings.json
        base_dir = Path(__file__).parent.parent
        config_dir = base_dir / "config"
        return str(config_dir / "settings.json")

    def _load_settings(self) -> Dict[str, Any]:
        """加载配置文件"""
        try:
            if os.path.exists(self._config_path):
                with open(self._config_path, 'r', encoding='utf-8') as f:
                    settings = json.load(f)
                logger.info(f"配置文件加载成功: {self._config_path}")
                #合并默认配置
                return self._merge_defaults(settings)
            else:
                logger.info("配置文件不存在，使用默认配置")
                return self.DEFAULT_SETTINGS.copy()
        except json.JSONDecodeError as e:
            logger.error(f"配置文件JSON格式错误: {e}")
            return self.DEFAULT_SETTINGS.copy()
        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
            return self.DEFAULT_SETTINGS.copy()

    def _merge_defaults(self, settings: Dict) -> Dict:
        """合并默认配置"""
        def merge_dict(base: dict, override: dict) -> dict:
            result = base.copy()
            for key, value in override.items():
                if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                    result[key] = merge_dict(result[key], value)
                else:
                    result[key] = value
            return result

        return merge_dict(self.DEFAULT_SETTINGS, settings)

    def _save_settings(self) -> bool:
        """保存配置到文件"""
        try:
            #确保目录存在
            config_dir = os.path.dirname(self._config_path)
            if config_dir and not os.path.exists(config_dir):
                os.makedirs(config_dir)

            with open(self._config_path, 'w', encoding='utf-8') as f:
                json.dump(self._settings, f, indent=2, ensure_ascii=False)
            logger.info(f"配置文件保存成功: {self._config_path}")
            return True
        except Exception as e:
            logger.error(f"保存配置文件失败: {e}")
            return False

    def show(self) -> Optional[Dict]:
        """
        显示配置对话框

        Returns:
            保存的配置字典，取消返回None
        """
        self.dialog = tk.Toplevel(self.parent)
        self.dialog.title("设置")
        self.dialog.geometry("500x550")
        self.dialog.resizable(False, False)
        self.dialog.transient(self.parent)
        self.dialog.grab_set()

        #居中显示
        self._center_dialog()

        #创建界面
        self._create_ui()

        #等待对话框关闭
        self.parent.wait_window(self.dialog)

        return self.result

    def _center_dialog(self):
        """居中显示对话框"""
        self.dialog.update_idletasks()
        width = self.dialog.winfo_width()
        height = self.dialog.winfo_height()
        x = (self.dialog.winfo_screenwidth() // 2) - (width // 2)
        y = (self.dialog.winfo_screenheight() // 2) - (height // 2)
        self.dialog.geometry(f"+{x}+{y}")

    def _create_ui(self):
        """创建用户界面"""
        #主框架
        main_frame = ttk.Frame(self.dialog, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        #创建Notebook（选项卡）
        notebook = ttk.Notebook(main_frame)
        notebook.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        #连接设置选项卡
        conn_frame = ttk.Frame(notebook, padding="10")
        notebook.add(conn_frame, text="连接设置")
        self._create_connection_tab(conn_frame)

        #存储设置选项卡
        storage_frame = ttk.Frame(notebook, padding="10")
        notebook.add(storage_frame, text="存储设置")
        self._create_storage_tab(storage_frame)

        #预览设置选项卡
        preview_frame = ttk.Frame(notebook, padding="10")
        notebook.add(preview_frame, text="预览设置")
        self._create_preview_tab(preview_frame)

        #日志设置选项卡
        log_frame = ttk.Frame(notebook, padding="10")
        notebook.add(log_frame, text="日志设置")
        self._create_log_tab(log_frame)

        #底部按钮
        self._create_buttons(main_frame)

    def _create_connection_tab(self, parent):
        """创建连接设置选项卡"""
        #服务器地址
        row = 0
        ttk.Label(parent, text="服务器地址:").grid(row=row, column=0, sticky=tk.W, pady=5)
        self._vars["conn_host"] = tk.StringVar(value=self._settings["connection"]["host"])
        host_entry = ttk.Entry(parent, textvariable=self._vars["conn_host"], width=30)
        host_entry.grid(row=row, column=1, sticky=tk.W, pady=5)

        #服务器端口
        row += 1
        ttk.Label(parent, text="服务器端口:").grid(row=row, column=0, sticky=tk.W, pady=5)
        self._vars["conn_port"] = tk.IntVar(value=self._settings["connection"]["port"])
        port_spinbox = ttk.Spinbox(parent, textvariable=self._vars["conn_port"],
                                   from_=1, to=65535, width=10)
        port_spinbox.grid(row=row, column=1, sticky=tk.W, pady=5)

        #连接超时
        row += 1
        ttk.Label(parent, text="连接超时(秒):").grid(row=row, column=0, sticky=tk.W, pady=5)
        self._vars["conn_timeout"] = tk.IntVar(value=self._settings["connection"]["timeout"])
        timeout_spinbox = ttk.Spinbox(parent, textvariable=self._vars["conn_timeout"],
                                      from_=1, to=60, width=10)
        timeout_spinbox.grid(row=row, column=1, sticky=tk.W, pady=5)

        #自动重连
        row += 1
        self._vars["conn_auto_reconnect"] = tk.BooleanVar(
            value=self._settings["connection"]["auto_reconnect"])
        auto_reconnect_check = ttk.Checkbutton(parent, text="自动重连",
                                                variable=self._vars["conn_auto_reconnect"])
        auto_reconnect_check.grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=5)

        #重连间隔
        row += 1
        ttk.Label(parent, text="重连间隔(秒):").grid(row=row, column=0, sticky=tk.W, pady=5)
        self._vars["conn_reconnect_interval"] = tk.IntVar(
            value=self._settings["connection"]["reconnect_interval"])
        reconnect_spinbox = ttk.Spinbox(parent, textvariable=self._vars["conn_reconnect_interval"],
                                        from_=1, to=60, width=10)
        reconnect_spinbox.grid(row=row, column=1, sticky=tk.W, pady=5)

    def _create_storage_tab(self, parent):
        """创建存储设置选项卡"""
        #图片保存路径
        row = 0
        ttk.Label(parent, text="图片保存路径:").grid(row=row, column=0, sticky=tk.W, pady=5)
        path_frame = ttk.Frame(parent)
        path_frame.grid(row=row, column=1, sticky=tk.W, pady=5)

        self._vars["storage_image_path"] = tk.StringVar(
            value=self._settings["storage"]["image_path"])
        image_path_entry = ttk.Entry(path_frame, textvariable=self._vars["storage_image_path"],
                                     width=25)
        image_path_entry.pack(side=tk.LEFT)
        ttk.Button(path_frame, text="浏览...",
                   command=lambda: self._browse_folder("storage_image_path")).pack(side=tk.LEFT, padx=5)

        #视频保存路径
        row += 1
        ttk.Label(parent, text="视频保存路径:").grid(row=row, column=0, sticky=tk.W, pady=5)
        video_path_frame = ttk.Frame(parent)
        video_path_frame.grid(row=row, column=1, sticky=tk.W, pady=5)

        self._vars["storage_video_path"] = tk.StringVar(
            value=self._settings["storage"]["video_path"])
        video_path_entry = ttk.Entry(video_path_frame, textvariable=self._vars["storage_video_path"],
                                     width=25)
        video_path_entry.pack(side=tk.LEFT)
        ttk.Button(video_path_frame, text="浏览...",
                   command=lambda: self._browse_folder("storage_video_path")).pack(side=tk.LEFT, padx=5)

        #JPEG质量
        row += 1
        ttk.Label(parent, text="JPEG质量:").grid(row=row, column=0, sticky=tk.W, pady=5)
        quality_frame = ttk.Frame(parent)
        quality_frame.grid(row=row, column=1, sticky=tk.W, pady=5)

        self._vars["storage_jpeg_quality"] = tk.IntVar(
            value=self._settings["storage"]["jpeg_quality"])
        quality_scale = ttk.Scale(quality_frame, from_=1, to=100,
                                  variable=self._vars["storage_jpeg_quality"],
                                  orient=tk.HORIZONTAL, length=150)
        quality_scale.pack(side=tk.LEFT)

        self._storage_quality_label = ttk.Label(quality_frame,
                                                 text=str(self._settings["storage"]["jpeg_quality"]))
        self._storage_quality_label.pack(side=tk.LEFT, padx=5)

        #绑定滑块值变化
        self._vars["storage_jpeg_quality"].trace_add("write",
            lambda *args: self._storage_quality_label.config(
                text=str(self._vars["storage_jpeg_quality"].get())))

    def _create_preview_tab(self, parent):
        """创建预览设置选项卡"""
        #默认分辨率
        row = 0
        ttk.Label(parent, text="默认分辨率:").grid(row=row, column=0, sticky=tk.W, pady=5)
        self._vars["preview_resolution"] = tk.StringVar(
            value=self.RESOLUTIONS[self._settings["preview"]["resolution_index"]])
        resolution_combo = ttk.Combobox(parent, textvariable=self._vars["preview_resolution"],
                                        values=self.RESOLUTIONS, state="readonly", width=15)
        resolution_combo.grid(row=row, column=1, sticky=tk.W, pady=5)

        #默认帧率
        row += 1
        ttk.Label(parent, text="默认帧率(fps):").grid(row=row, column=0, sticky=tk.W, pady=5)
        self._vars["preview_fps"] = tk.IntVar(value=self._settings["preview"]["fps"])
        fps_spinbox = ttk.Spinbox(parent, textvariable=self._vars["preview_fps"],
                                  from_=1, to=30, width=10)
        fps_spinbox.grid(row=row, column=1, sticky=tk.W, pady=5)

        #JPEG质量
        row += 1
        ttk.Label(parent, text="JPEG质量:").grid(row=row, column=0, sticky=tk.W, pady=5)
        preview_quality_frame = ttk.Frame(parent)
        preview_quality_frame.grid(row=row, column=1, sticky=tk.W, pady=5)

        self._vars["preview_jpeg_quality"] = tk.IntVar(
            value=self._settings["preview"]["jpeg_quality"])
        preview_quality_scale = ttk.Scale(preview_quality_frame, from_=1, to=100,
                                          variable=self._vars["preview_jpeg_quality"],
                                          orient=tk.HORIZONTAL, length=150)
        preview_quality_scale.pack(side=tk.LEFT)

        self._preview_quality_label = ttk.Label(preview_quality_frame,
                                                 text=str(self._settings["preview"]["jpeg_quality"]))
        self._preview_quality_label.pack(side=tk.LEFT, padx=5)

        #绑定滑块值变化
        self._vars["preview_jpeg_quality"].trace_add("write",
            lambda *args: self._preview_quality_label.config(
                text=str(self._vars["preview_jpeg_quality"].get())))

    def _create_log_tab(self, parent):
        """创建日志设置选项卡"""
        #日志级别
        row = 0
        ttk.Label(parent, text="日志级别:").grid(row=row, column=0, sticky=tk.W, pady=5)
        self._vars["log_level"] = tk.StringVar(value=self._settings["log"]["level"])
        level_combo = ttk.Combobox(parent, textvariable=self._vars["log_level"],
                                   values=self.LOG_LEVELS, state="readonly", width=15)
        level_combo.grid(row=row, column=1, sticky=tk.W, pady=5)

        #日志保存路径
        row += 1
        ttk.Label(parent, text="日志保存路径:").grid(row=row, column=0, sticky=tk.W, pady=5)
        log_path_frame = ttk.Frame(parent)
        log_path_frame.grid(row=row, column=1, sticky=tk.W, pady=5)

        self._vars["log_path"] = tk.StringVar(value=self._settings["log"]["path"])
        log_path_entry = ttk.Entry(log_path_frame, textvariable=self._vars["log_path"], width=25)
        log_path_entry.pack(side=tk.LEFT)
        ttk.Button(log_path_frame, text="浏览...",
                   command=lambda: self._browse_folder("log_path")).pack(side=tk.LEFT, padx=5)

        #单文件最大大小
        row += 1
        ttk.Label(parent, text="单文件大小(MB):").grid(row=row, column=0, sticky=tk.W, pady=5)
        self._vars["log_max_size"] = tk.IntVar(value=self._settings["log"]["max_size_mb"])
        max_size_spinbox = ttk.Spinbox(parent, textvariable=self._vars["log_max_size"],
                                       from_=1, to=100, width=10)
        max_size_spinbox.grid(row=row, column=1, sticky=tk.W, pady=5)

        #保留文件数
        row += 1
        ttk.Label(parent, text="保留文件数:").grid(row=row, column=0, sticky=tk.W, pady=5)
        self._vars["log_backup_count"] = tk.IntVar(value=self._settings["log"]["backup_count"])
        backup_spinbox = ttk.Spinbox(parent, textvariable=self._vars["log_backup_count"],
                                     from_=1, to=50, width=10)
        backup_spinbox.grid(row=row, column=1, sticky=tk.W, pady=5)

    def _create_buttons(self, parent):
        """创建底部按钮"""
        btn_frame = ttk.Frame(parent)
        btn_frame.pack(fill=tk.X)

        #恢复默认按钮
        ttk.Button(btn_frame, text="恢复默认", command=self._on_reset).pack(side=tk.LEFT)

        #取消按钮
        ttk.Button(btn_frame, text="取消", command=self._on_cancel).pack(side=tk.RIGHT, padx=5)

        #保存按钮
        ttk.Button(btn_frame, text="保存", command=self._on_save).pack(side=tk.RIGHT)

    def _browse_folder(self, var_name: str):
        """浏览文件夹"""
        current_path = self._vars[var_name].get()
        initial_dir = current_path if os.path.isdir(current_path) else "."

        folder = filedialog.askdirectory(
            parent=self.dialog,
            initialdir=initial_dir,
            title="选择文件夹"
        )

        if folder:
            self._vars[var_name].set(folder)

    def _validate_settings(self) -> list:
        """
        验证配置

        Returns:
            错误信息列表
        """
        errors = []

        #验证端口
        try:
            port = self._vars["conn_port"].get()
            if not 1 <= port <= 65535:
                errors.append(f"端口号无效: {port}，有效范围1-65535")
        except tk.TclError:
            errors.append("端口号必须是数字")

        #验证超时
        try:
            timeout = self._vars["conn_timeout"].get()
            if timeout <= 0:
                errors.append(f"连接超时无效: {timeout}，必须大于0")
        except tk.TclError:
            errors.append("连接超时必须是数字")

        #验证帧率
        try:
            fps = self._vars["preview_fps"].get()
            if not 1 <= fps <= 30:
                errors.append(f"帧率无效: {fps}，有效范围1-30")
        except tk.TclError:
            errors.append("帧率必须是数字")

        #验证JPEG质量
        try:
            storage_quality = self._vars["storage_jpeg_quality"].get()
            if not 1 <= storage_quality <= 100:
                errors.append(f"存储JPEG质量无效: {storage_quality}，有效范围1-100")
        except tk.TclError:
            errors.append("存储JPEG质量必须是数字")

        try:
            preview_quality = self._vars["preview_jpeg_quality"].get()
            if not 1 <= preview_quality <= 100:
                errors.append(f"预览JPEG质量无效: {preview_quality}，有效范围1-100")
        except tk.TclError:
            errors.append("预览JPEG质量必须是数字")

        return errors

    def _collect_settings(self) -> Dict:
        """收集界面上的配置值"""
        #获取分辨率索引
        resolution_str = self._vars["preview_resolution"].get()
        try:
            resolution_index = self.RESOLUTIONS.index(resolution_str)
        except ValueError:
            resolution_index = 0

        return {
            "connection": {
                "host": self._vars["conn_host"].get(),
                "port": self._vars["conn_port"].get(),
                "timeout": self._vars["conn_timeout"].get(),
                "auto_reconnect": self._vars["conn_auto_reconnect"].get(),
                "reconnect_interval": self._vars["conn_reconnect_interval"].get()
            },
            "storage": {
                "image_path": self._vars["storage_image_path"].get(),
                "video_path": self._vars["storage_video_path"].get(),
                "jpeg_quality": self._vars["storage_jpeg_quality"].get()
            },
            "preview": {
                "resolution_index": resolution_index,
                "fps": self._vars["preview_fps"].get(),
                "jpeg_quality": self._vars["preview_jpeg_quality"].get()
            },
            "log": {
                "level": self._vars["log_level"].get(),
                "path": self._vars["log_path"].get(),
                "max_size_mb": self._vars["log_max_size"].get(),
                "backup_count": self._vars["log_backup_count"].get()
            }
        }

    def _on_save(self):
        """保存按钮点击"""
        #验证配置
        errors = self._validate_settings()
        if errors:
            messagebox.showerror("配置错误", "\n".join(errors), parent=self.dialog)
            return

        #收集配置
        self._settings = self._collect_settings()

        #保存到文件
        if self._save_settings():
            self.result = self._settings

            #调用回调
            if self.on_save:
                self.on_save(self._settings)

            messagebox.showinfo("成功", "配置已保存", parent=self.dialog)
            self.dialog.destroy()
        else:
            messagebox.showerror("错误", "保存配置失败", parent=self.dialog)

    def _on_cancel(self):
        """取消按钮点击"""
        self.result = None
        self.dialog.destroy()

    def _on_reset(self):
        """恢复默认按钮点击"""
        if messagebox.askyesno("确认", "确定要恢复默认配置吗？", parent=self.dialog):
            self._settings = self.DEFAULT_SETTINGS.copy()
            self._update_ui_from_settings()
            logger.info("配置已恢复为默认值")

    def _update_ui_from_settings(self):
        """从配置更新界面"""
        #连接设置
        self._vars["conn_host"].set(self._settings["connection"]["host"])
        self._vars["conn_port"].set(self._settings["connection"]["port"])
        self._vars["conn_timeout"].set(self._settings["connection"]["timeout"])
        self._vars["conn_auto_reconnect"].set(self._settings["connection"]["auto_reconnect"])
        self._vars["conn_reconnect_interval"].set(self._settings["connection"]["reconnect_interval"])

        #存储设置
        self._vars["storage_image_path"].set(self._settings["storage"]["image_path"])
        self._vars["storage_video_path"].set(self._settings["storage"]["video_path"])
        self._vars["storage_jpeg_quality"].set(self._settings["storage"]["jpeg_quality"])

        #预览设置
        resolution_index = self._settings["preview"]["resolution_index"]
        if 0 <= resolution_index < len(self.RESOLUTIONS):
            self._vars["preview_resolution"].set(self.RESOLUTIONS[resolution_index])
        self._vars["preview_fps"].set(self._settings["preview"]["fps"])
        self._vars["preview_jpeg_quality"].set(self._settings["preview"]["jpeg_quality"])

        #日志设置
        self._vars["log_level"].set(self._settings["log"]["level"])
        self._vars["log_path"].set(self._settings["log"]["path"])
        self._vars["log_max_size"].set(self._settings["log"]["max_size_mb"])
        self._vars["log_backup_count"].set(self._settings["log"]["backup_count"])

    @classmethod
    def get_settings(cls, config_path: Optional[str] = None) -> Dict:
        """
        获取当前配置（不显示对话框）

        Args:
            config_path: 配置文件路径，为None时使用默认路径

        Returns:
            配置字典
        """
        if config_path is None:
            base_dir = Path(__file__).parent.parent
            config_path = str(base_dir / "config" / "settings.json")

        try:
            if os.path.exists(config_path):
                with open(config_path, 'r', encoding='utf-8') as f:
                    settings = json.load(f)
                #合并默认配置
                def merge_dict(base: dict, override: dict) -> dict:
                    result = base.copy()
                    for key, value in override.items():
                        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                            result[key] = merge_dict(result[key], value)
                        else:
                            result[key] = value
                    return result
                return merge_dict(cls.DEFAULT_SETTINGS, settings)
        except Exception as e:
            logger.error(f"加载配置失败: {e}")

        return cls.DEFAULT_SETTINGS.copy()


#测试代码
if __name__ == '__main__':
    import sys
    logger.remove()
    logger.add(sys.stdout, level="DEBUG")

    root = tk.Tk()
    root.withdraw()

    def on_save(settings):
        print(f"配置已保存: {settings}")

    dialog = SettingsDialog(root, on_save=on_save)
    result = dialog.show()

    if result:
        print(f"返回配置: {result}")
    else:
        print("用户取消")

    root.destroy()
