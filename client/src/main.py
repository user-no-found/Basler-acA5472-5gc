# -*- coding: utf-8 -*-
"""
Basler 相机控制系统 - 客户端程序入口

功能:
- 初始化日志系统
- 初始化相机控制器
- 初始化图像处理器
- 初始化图像采集器
- 启动TCP服务器
"""

import sys
import os
import asyncio
from pathlib import Path

# 添加src目录到路径
src_dir = Path(__file__).parent
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from loguru import logger


def setup_logger():
    """配置日志系统"""
    # 移除默认处理器
    logger.remove()

    # 控制台输出
    logger.add(
        sys.stdout,
        level="DEBUG",
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
    )

    # 日志文件
    log_dir = src_dir.parent / "logs"
    log_dir.mkdir(exist_ok=True)

    logger.add(
        log_dir / "client_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        rotation="10 MB",      # 每10MB滚动
        retention="10 days",   # 保留10天
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}"
    )

    logger.info("日志系统初始化完成")


async def main():
    """主函数"""
    # 配置日志
    setup_logger()

    logger.info("=" * 50)
    logger.info("Basler 相机控制系统 - 客户端")
    logger.info("=" * 50)

    server = None
    try:
        # 导入模块
        from tcp_server import TCPServer
        from camera_controller import CameraController
        from image_processor import ImageProcessor
        from image_acquisition import ImageAcquisition, PreviewAcquisition
        from config_manager import ConfigManager
        import sensor_data
        import photo_monitor

        # 加载配置
        config_path = src_dir.parent / "config" / "config.json"
        config_manager = ConfigManager(str(config_path))
        config = config_manager.config

        # 初始化相机控制器
        logger.info("初始化相机控制器...")
        camera = CameraController()

        # 尝试连接相机
        success, error_code = camera.connect()
        if success:
            logger.info("相机连接成功")
        else:
            logger.error(f"相机连接失败 (错误码: 0x{error_code:04X})")
            logger.error("请检查: 1) 相机是否已连接 2) pylon Viewer是否已关闭 3) USB/网线是否正常")
            sys.exit(1)

        # 初始化图像处理器
        logger.info("初始化图像处理器...")
        processor = ImageProcessor(config_manager=config_manager)

        # 初始化图像采集器
        logger.info("初始化图像采集器...")
        acquisition = ImageAcquisition(camera)

        # 初始化预览采集器
        logger.info("初始化预览采集器...")
        preview = PreviewAcquisition(camera)

        # 初始化TCP服务器
        host = config.get("server", {}).get("host", "0.0.0.0")
        port = config.get("server", {}).get("port", 8899)
        logger.info(f"初始化TCP服务器: {host}:{port}")
        server = TCPServer(host=host, port=port)

        # 绑定组件
        server.set_camera(camera)
        server.set_image_processor(processor)
        server.set_image_acquisition(acquisition)
        server.set_preview_acquisition(preview)

        # 启动传感器数据接收线程
        sensor_data_mode = config.get("sensor_data", {}).get("mode", "110")
        sensor_data_address = config.get("sensor_data", {}).get("address", "192.168.1.201")
        sensor_data_port = config.get("sensor_data", {}).get("port", 2000)
        sensor_data.start_sensor_data_receiver(
            address=(sensor_data_address, sensor_data_port),
            mode=sensor_data_mode
        )

        # 启动照片目录监控
        photo_monitor.start_photo_monitor(processor._save_path)

        # 启动服务器
        logger.info("启动TCP服务器...")
        await server.start()

    except ImportError as e:
        logger.error(f"导入模块失败: {e}")
        logger.error("请确保已安装所有依赖: pip install -r requirements.txt")
        sys.exit(1)

    except KeyboardInterrupt:
        logger.info("收到中断信号，正在关闭...")

    except Exception as e:
        logger.exception(f"程序异常退出: {e}")
        sys.exit(1)

    finally:
        # 清理资源
        import sensor_data
        import photo_monitor
        sensor_data.stop_sensor_data_receiver()
        photo_monitor.stop_photo_monitor()
        if server is not None:
            try:
                await server.stop()
            except Exception as e:
                logger.warning(f"停止TCP服务器时发生异常: {e}")

    logger.info("程序正常退出")


if __name__ == '__main__':
    asyncio.run(main())
