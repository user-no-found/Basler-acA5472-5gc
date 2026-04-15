# -*- coding: utf-8 -*-
"""
EXIF 写入模块

负责：
- 调用系统 ExifTool 为 JPG 照片写入 GPS、高度、UTC 时间等元数据
- 从 sensor_data 模块获取最新传感器数据
- 支持重试机制，与 Deep_sea_observation 项目行为保持一致

依赖：
- 系统 PATH 中需要存在 `exiftool`（Linux/macOS）或 `exiftool.exe`（Windows）
"""

import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from loguru import logger

import sensor_data


def _exiftool_available() -> bool:
    """检查系统中是否安装了 ExifTool"""
    return shutil.which("exiftool") is not None


def modify_photo_exif(photo_path: Path) -> bool:
    """
    使用 ExifTool 修改照片 EXIF 属性

    Args:
        photo_path: 照片文件的完整路径

    Returns:
        bool: 是否成功
    """
    if not _exiftool_available():
        logger.error("系统中未找到 ExifTool，无法写入 EXIF。请确保 exiftool 已在 PATH 中。")
        return False

    max_retries = 5
    retry_delay_ms = 500

    for attempt in range(1, max_retries + 1):
        gps_data = sensor_data.get_gps_height_utc()
        if gps_data is None:
            logger.error(f"无法获取 GPS 和 UTC 数据 (尝试 {attempt}/{max_retries})，等待 {retry_delay_ms}ms 后重试")
            time.sleep(retry_delay_ms / 1000.0)
            continue

        (
            latitude, longitude, height,
            utc_year, utc_month, utc_day,
            utc_hour, utc_minute, utc_second
        ) = gps_data

        # 格式化 UTC 时间为 ExifTool 所需格式: "YYYY:MM:DD HH:MM:SS"
        try:
            utc_seconds_f32 = float(utc_second)
        except (ValueError, TypeError):
            utc_seconds_f32 = 0.0
        seconds = int(utc_seconds_f32)

        year_str = utc_year if len(utc_year) != 2 else f"20{utc_year}"
        try:
            year = int(year_str)
            month = int(utc_month)
            day = int(utc_day)
            hour = int(utc_hour)
            minute = int(utc_minute)
        except (ValueError, TypeError):
            logger.error(f"UTC 时间字段格式异常，跳过照片: {photo_path}")
            return False

        formatted_time = f"{year:04d}:{month:02d}:{day:02d} {hour:02d}:{minute:02d}:{seconds:02d}"
        logger.debug(f"格式化后的时间: {formatted_time}")

        # 根据高度值判断是在海平面以上还是以下
        try:
            height_f = float(height)
        except (ValueError, TypeError):
            height_f = 0.0
        altitude_ref = "0" if height_f >= 0.0 else "1"

        cmd = [
            "exiftool",
            "-overwrite_original",
            f"-GPSLatitude={latitude}",
            f"-GPSLongitude={longitude}",
            f"-GPSAltitude={height}",
            f"-GPSAltitudeRef={altitude_ref}",
            f"-GPSTimeStamp={hour:02d}:{minute:02d}:{seconds:02d}",
            f"-DateTime={formatted_time}",
            f"-DateTimeOriginal={formatted_time}",
            f"-CreateDate={formatted_time}",
            f"-ModifyDate={formatted_time}",
            f"-DateTimeDigitized={formatted_time}",
            str(photo_path),
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode == 0:
                logger.debug(f"成功修改照片 EXIF 属性: {photo_path}")
                return True
            else:
                logger.error(f"修改照片 EXIF 属性失败 (exiftool 返回 {result.returncode}): {result.stderr.strip()}")
                return False
        except Exception as e:
            logger.error(f"调用 ExifTool 异常: {e}")
            return False

    logger.error(f"获取 GPS 和 UTC 数据失败，已达到最大重试次数，跳过照片: {photo_path}")
    return False
