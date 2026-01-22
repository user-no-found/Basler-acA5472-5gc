# -*- coding: utf-8 -*-
"""
配置管理模块
负责加载、保存和管理系统配置
"""

import json
import os
from typing import Any, Dict, Optional, List, Tuple
from pathlib import Path
from loguru import logger


class ConfigManager:
    """配置管理器"""

    #默认配置
    DEFAULT_CONFIG = {
        "protocol_version": "2.0",
        "tcp": {
            "host": "0.0.0.0",
            "port": 8899,
            "timeout": 30,
            "max_clients": 5
        },
        "camera": {
            "serial": "",
            "default_exposure": 10000,
            "default_gain": 100,
            "reconnect_interval": 5,
            "grab_timeout": 5000
        },
        "storage": {
            "image_path": "./images",
            "video_path": "./videos",
            "image_format": "jpg",
            "jpeg_quality": 95
        },
        "preview": {
            "default_resolution": [1920, 1080],
            "default_fps": 10,
            "jpeg_quality": 80,
            "max_fps": 30
        },
        "video": {
            "codec": "H264",
            "default_fps": 5,
            "supported_resolutions": [
                [1920, 1080],
                [1280, 720],
                [640, 480]
            ]
        }
    }

    def __init__(self, config_path: Optional[str] = None):
        """
        初始化配置管理器

        Args:
            config_path: 配置文件路径，为None时使用默认路径
        """
        if config_path is None:
            #默认配置文件路径
            base_dir = Path(__file__).parent.parent
            config_path = str(base_dir / "config" / "config.json")

        self._config_path = config_path
        self._config: Dict[str, Any] = {}
        self._load_config()

    def _load_config(self) -> None:
        """加载配置文件"""
        try:
            if os.path.exists(self._config_path):
                with open(self._config_path, 'r', encoding='utf-8') as f:
                    self._config = json.load(f)
                logger.info(f"配置文件加载成功: {self._config_path}")
                #合并默认配置（补充缺失项）
                self._merge_defaults()
            else:
                logger.warning(f"配置文件不存在，使用默认配置: {self._config_path}")
                self._config = self.DEFAULT_CONFIG.copy()
                self._save_config()
        except json.JSONDecodeError as e:
            logger.error(f"配置文件JSON格式错误: {e}")
            self._config = self.DEFAULT_CONFIG.copy()
        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
            self._config = self.DEFAULT_CONFIG.copy()

    def _merge_defaults(self) -> None:
        """合并默认配置，补充缺失的配置项"""
        def merge_dict(base: dict, override: dict) -> dict:
            result = base.copy()
            for key, value in override.items():
                if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                    result[key] = merge_dict(result[key], value)
                else:
                    result[key] = value
            return result

        self._config = merge_dict(self.DEFAULT_CONFIG, self._config)

    def _save_config(self) -> bool:
        """
        保存配置到文件

        Returns:
            是否保存成功
        """
        try:
            #确保目录存在
            config_dir = os.path.dirname(self._config_path)
            if config_dir and not os.path.exists(config_dir):
                os.makedirs(config_dir)

            with open(self._config_path, 'w', encoding='utf-8') as f:
                json.dump(self._config, f, indent=2, ensure_ascii=False)
            logger.info(f"配置文件保存成功: {self._config_path}")
            return True
        except Exception as e:
            logger.error(f"保存配置文件失败: {e}")
            return False

    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置项（支持点号分隔的嵌套键）

        Args:
            key: 配置键，如 "tcp.port" 或 "camera.default_exposure"
            default: 默认值

        Returns:
            配置值
        """
        keys = key.split('.')
        value = self._config

        try:
            for k in keys:
                value = value[k]
            return value
        except (KeyError, TypeError):
            return default

    def set(self, key: str, value: Any, save: bool = True) -> bool:
        """
        设置配置项（支持点号分隔的嵌套键）

        Args:
            key: 配置键
            value: 配置值
            save: 是否立即保存到文件

        Returns:
            是否设置成功
        """
        keys = key.split('.')
        config = self._config

        try:
            #遍历到倒数第二层
            for k in keys[:-1]:
                if k not in config:
                    config[k] = {}
                config = config[k]

            #设置最后一层的值
            config[keys[-1]] = value
            logger.debug(f"配置项已更新: {key} = {value}")

            if save:
                return self._save_config()
            return True
        except Exception as e:
            logger.error(f"设置配置项失败: {key} = {value}, 错误: {e}")
            return False

    def reload(self) -> None:
        """重新加载配置文件"""
        self._load_config()

    @property
    def config(self) -> Dict[str, Any]:
        """获取完整配置字典"""
        return self._config

    @property
    def tcp_host(self) -> str:
        """TCP服务器主机地址"""
        return self.get("tcp.host", "0.0.0.0")

    @property
    def tcp_port(self) -> int:
        """TCP服务器端口"""
        return self.get("tcp.port", 8899)

    @property
    def tcp_timeout(self) -> int:
        """TCP超时时间（秒）"""
        return self.get("tcp.timeout", 30)

    @property
    def tcp_max_clients(self) -> int:
        """最大客户端连接数"""
        return self.get("tcp.max_clients", 5)

    @property
    def camera_serial(self) -> str:
        """相机序列号"""
        return self.get("camera.serial", "")

    @property
    def camera_default_exposure(self) -> int:
        """默认曝光时间（微秒）"""
        return self.get("camera.default_exposure", 10000)

    @property
    def camera_default_gain(self) -> int:
        """默认增益"""
        return self.get("camera.default_gain", 100)

    @property
    def camera_reconnect_interval(self) -> int:
        """相机重连间隔（秒）"""
        return self.get("camera.reconnect_interval", 5)

    @property
    def camera_grab_timeout(self) -> int:
        """图像采集超时（毫秒）"""
        return self.get("camera.grab_timeout", 5000)

    @property
    def storage_image_path(self) -> str:
        """图像存储路径"""
        return self.get("storage.image_path", "./images")

    @property
    def storage_video_path(self) -> str:
        """视频存储路径"""
        return self.get("storage.video_path", "./videos")

    @property
    def storage_image_format(self) -> str:
        """图像格式"""
        return self.get("storage.image_format", "jpg")

    @property
    def storage_jpeg_quality(self) -> int:
        """JPEG质量"""
        return self.get("storage.jpeg_quality", 95)

    @property
    def preview_default_resolution(self) -> List[int]:
        """预览默认分辨率"""
        return self.get("preview.default_resolution", [1920, 1080])

    @property
    def preview_default_fps(self) -> int:
        """预览默认帧率"""
        return self.get("preview.default_fps", 10)

    @property
    def preview_jpeg_quality(self) -> int:
        """预览JPEG质量"""
        return self.get("preview.jpeg_quality", 80)

    @property
    def preview_max_fps(self) -> int:
        """预览最大帧率"""
        return self.get("preview.max_fps", 30)

    @property
    def video_codec(self) -> str:
        """视频编码格式"""
        return self.get("video.codec", "H264")

    @property
    def video_default_fps(self) -> int:
        """视频默认帧率"""
        return self.get("video.default_fps", 5)

    @property
    def video_supported_resolutions(self) -> List[List[int]]:
        """支持的视频分辨率列表"""
        return self.get("video.supported_resolutions", [[1920, 1080], [1280, 720], [640, 480]])

    def get_all(self) -> Dict[str, Any]:
        """获取完整配置"""
        return self._config.copy()

    def validate_config(self) -> List[str]:
        """
        验证配置，返回错误列表

        Returns:
            错误信息列表，空列表表示配置有效
        """
        errors = []

        #TCP配置验证
        tcp_port = self.tcp_port
        if not isinstance(tcp_port, int) or not 1 <= tcp_port <= 65535:
            errors.append(f"TCP端口无效: {tcp_port}，有效范围1-65535")

        tcp_timeout = self.tcp_timeout
        if not isinstance(tcp_timeout, (int, float)) or tcp_timeout <= 0:
            errors.append(f"TCP超时时间无效: {tcp_timeout}，必须大于0")

        tcp_max_clients = self.tcp_max_clients
        if not isinstance(tcp_max_clients, int) or tcp_max_clients < 1:
            errors.append(f"最大客户端数无效: {tcp_max_clients}，必须大于等于1")

        #相机配置验证
        exposure = self.camera_default_exposure
        if not isinstance(exposure, (int, float)) or exposure < 0:
            errors.append(f"曝光时间不能为负: {exposure}")

        gain = self.camera_default_gain
        if not isinstance(gain, (int, float)) or gain < 0:
            errors.append(f"增益不能为负: {gain}")

        reconnect_interval = self.camera_reconnect_interval
        if not isinstance(reconnect_interval, (int, float)) or reconnect_interval <= 0:
            errors.append(f"重连间隔无效: {reconnect_interval}，必须大于0")

        grab_timeout = self.camera_grab_timeout
        if not isinstance(grab_timeout, (int, float)) or grab_timeout <= 0:
            errors.append(f"采集超时无效: {grab_timeout}，必须大于0")

        #存储配置验证
        image_path = self.storage_image_path
        if not image_path:
            errors.append("图像存储路径不能为空")

        video_path = self.storage_video_path
        if not video_path:
            errors.append("视频存储路径不能为空")

        jpeg_quality = self.storage_jpeg_quality
        if not isinstance(jpeg_quality, int) or not 1 <= jpeg_quality <= 100:
            errors.append(f"JPEG质量无效: {jpeg_quality}，有效范围1-100")

        #预览配置验证
        preview_resolution = self.preview_default_resolution
        if not isinstance(preview_resolution, list) or len(preview_resolution) != 2:
            errors.append(f"预览分辨率格式无效: {preview_resolution}")
        elif preview_resolution[0] <= 0 or preview_resolution[1] <= 0:
            errors.append(f"预览分辨率值无效: {preview_resolution}")

        preview_fps = self.preview_default_fps
        if not isinstance(preview_fps, int) or preview_fps <= 0:
            errors.append(f"预览帧率无效: {preview_fps}，必须大于0")

        preview_max_fps = self.preview_max_fps
        if not isinstance(preview_max_fps, int) or preview_max_fps <= 0:
            errors.append(f"预览最大帧率无效: {preview_max_fps}，必须大于0")

        preview_jpeg_quality = self.preview_jpeg_quality
        if not isinstance(preview_jpeg_quality, int) or not 1 <= preview_jpeg_quality <= 100:
            errors.append(f"预览JPEG质量无效: {preview_jpeg_quality}，有效范围1-100")

        #视频配置验证
        video_fps = self.video_default_fps
        if not isinstance(video_fps, int) or video_fps <= 0:
            errors.append(f"视频帧率无效: {video_fps}，必须大于0")

        video_codec = self.video_codec
        supported_codecs = ["H264", "H265", "MJPEG"]
        if video_codec not in supported_codecs:
            errors.append(f"视频编码格式无效: {video_codec}，支持: {supported_codecs}")

        return errors

    def normalize_paths(self) -> None:
        """
        规范化存储路径（转换为绝对路径）
        """
        #图像路径
        image_path = self.storage_image_path
        if image_path and not os.path.isabs(image_path):
            abs_path = os.path.abspath(image_path)
            self.set("storage.image_path", abs_path, save=False)
            logger.info(f"图像路径已转换为绝对路径: {abs_path}")

        #视频路径
        video_path = self.storage_video_path
        if video_path and not os.path.isabs(video_path):
            abs_path = os.path.abspath(video_path)
            self.set("storage.video_path", abs_path, save=False)
            logger.info(f"视频路径已转换为绝对路径: {abs_path}")

    def reset_to_defaults(self) -> bool:
        """
        重置为默认配置

        Returns:
            是否重置成功
        """
        self._config = self.DEFAULT_CONFIG.copy()
        logger.info("配置已重置为默认值")
        return self._save_config()

    def ensure_storage_dirs(self) -> None:
        """确保存储目录存在"""
        for path_key in ["storage.image_path", "storage.video_path"]:
            path = self.get(path_key)
            if path and not os.path.exists(path):
                try:
                    os.makedirs(path)
                    logger.info(f"创建存储目录: {path}")
                except Exception as e:
                    logger.error(f"创建存储目录失败: {path}, 错误: {e}")


#全局配置实例
_config_instance: Optional[ConfigManager] = None


def get_config(config_path: Optional[str] = None) -> ConfigManager:
    """
    获取全局配置实例（单例模式）

    Args:
        config_path: 配置文件路径（仅首次调用时有效）

    Returns:
        配置管理器实例
    """
    global _config_instance
    if _config_instance is None:
        _config_instance = ConfigManager(config_path)
    return _config_instance
