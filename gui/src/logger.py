"""
上位机日志模块
使用loguru实现分级日志、文件滚动、彩色控制台输出
"""
import sys
import os
from pathlib import Path
from typing import Optional
from loguru import logger


#默认日志目录
DEFAULT_LOG_DIR = "./logs"
#默认日志级别
DEFAULT_LOG_LEVEL = "INFO"
#默认控制台日志级别
DEFAULT_CONSOLE_LEVEL = "INFO"
#默认单文件最大大小
DEFAULT_ROTATION = "10 MB"
#默认保留文件数
DEFAULT_RETENTION = 10


def setup_logger(
    log_dir: str = DEFAULT_LOG_DIR,
    log_level: str = DEFAULT_LOG_LEVEL,
    rotation: str = DEFAULT_ROTATION,
    retention: int = DEFAULT_RETENTION,
    console_level: str = DEFAULT_CONSOLE_LEVEL,
    app_name: str = "gui"
) -> "logger":
    """
    配置日志系统

    Args:
        log_dir: 日志文件目录
        log_level: 文件日志级别（DEBUG/INFO/WARNING/ERROR）
        rotation: 单文件最大大小（如"10 MB"）
        retention: 保留文件数量
        console_level: 控制台日志级别
        app_name: 应用名称，用于日志文件前缀

    Returns:
        配置好的logger实例
    """
    #移除默认处理器
    logger.remove()

    #确保日志目录存在
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    #添加控制台输出（彩色）
    logger.add(
        sys.stderr,
        level=console_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
               "<level>{level: <8}</level> | "
               "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
               "<level>{message}</level>",
        colorize=True
    )

    #添加文件输出（按日期命名，自动滚动）
    logger.add(
        f"{log_dir}/{app_name}_{{time:YYYY-MM-DD}}.log",
        level=log_level,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
        rotation=rotation,
        retention=retention,
        encoding="utf-8",
        enqueue=True  #线程安全
    )

    #添加错误日志单独文件
    logger.add(
        f"{log_dir}/{app_name}_error_{{time:YYYY-MM-DD}}.log",
        level="ERROR",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
        rotation=rotation,
        retention=retention,
        encoding="utf-8",
        enqueue=True
    )

    logger.info(f"日志系统初始化完成 - 目录: {log_path.absolute()}, 级别: {log_level}")

    return logger


def get_logger(module_name: Optional[str] = None) -> "logger":
    """
    获取logger实例

    Args:
        module_name: 模块名称（可选，用于日志标识）

    Returns:
        logger实例
    """
    if module_name:
        return logger.bind(name=module_name)
    return logger


#模块级别的便捷函数
def debug(message: str, *args, **kwargs):
    """记录DEBUG级别日志"""
    logger.debug(message, *args, **kwargs)


def info(message: str, *args, **kwargs):
    """记录INFO级别日志"""
    logger.info(message, *args, **kwargs)


def warning(message: str, *args, **kwargs):
    """记录WARNING级别日志"""
    logger.warning(message, *args, **kwargs)


def error(message: str, *args, **kwargs):
    """记录ERROR级别日志"""
    logger.error(message, *args, **kwargs)


def exception(message: str, *args, **kwargs):
    """记录异常日志（包含堆栈信息）"""
    logger.exception(message, *args, **kwargs)


#导出logger实例供直接使用
__all__ = [
    'logger',
    'setup_logger',
    'get_logger',
    'debug',
    'info',
    'warning',
    'error',
    'exception',
]
