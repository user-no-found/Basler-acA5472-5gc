#-*- coding: utf-8 -*-
"""
Basler 相机控制系统 - 上位机程序入口

功能:
- 初始化日志系统
- 启动主窗口
"""

import sys
import os
from pathlib import Path

#添加src目录到路径
src_dir = Path(__file__).parent
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from loguru import logger


def setup_logger():
    """配置日志系统"""
    #移除默认处理器
    logger.remove()

    #控制台输出
    logger.add(
        sys.stdout,
        level="DEBUG",
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
    )

    #日志文件
    log_dir = src_dir.parent / "logs"
    log_dir.mkdir(exist_ok=True)

    logger.add(
        log_dir / "gui_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        rotation="10 MB",      #每10MB滚动
        retention="10 days",   #保留10天
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}"
    )

    logger.info("日志系统初始化完成")


def main():
    """主函数"""
    #配置日志
    setup_logger()

    logger.info("=" * 50)
    logger.info("Basler 相机控制系统 - 上位机")
    logger.info("=" * 50)

    try:
        #导入主窗口
        from main_window import MainWindow

        #创建并运行主窗口
        window = MainWindow()
        window.run()

    except ImportError as e:
        logger.error(f"导入模块失败: {e}")
        logger.error("请确保已安装所有依赖: pip install loguru Pillow")
        sys.exit(1)

    except Exception as e:
        logger.exception(f"程序异常退出: {e}")
        sys.exit(1)

    logger.info("程序正常退出")


if __name__ == '__main__':
    main()
