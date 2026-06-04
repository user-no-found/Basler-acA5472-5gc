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
from typing import Optional, Tuple

from loguru import logger

import sensor_data

GpsExifData = Tuple[str, str, str, str, str, str, str, str, str]

# Fixed optical metadata for this client:
# Basler acA5472-5gc + Basler Lens C12-1224-25M.
CAMERA_MAKE = "Basler"
CAMERA_MODEL = "acA5472-5gc"
LENS_MAKE = "Basler"
LENS_MODEL = "Basler Lens C12-1224-25M"
FOCAL_LENGTH_MM = 12.0
SENSOR_WIDTH_MM = 13.13
SENSOR_HEIGHT_MM = 8.76
PIXEL_SIZE_UM = 2.40
FOCAL_PLANE_RESOLUTION_PPI = 25400.0 / PIXEL_SIZE_UM
EXIF_EQUIPMENT_COMMENT = (
    f"Camera={CAMERA_MAKE} {CAMERA_MODEL}; "
    f"Lens={LENS_MODEL}; "
    f"FocalLength={FOCAL_LENGTH_MM:.1f} mm; "
    f"SensorSize={SENSOR_WIDTH_MM:.2f}x{SENSOR_HEIGHT_MM:.2f} mm; "
    f"PixelSize={PIXEL_SIZE_UM:.2f}x{PIXEL_SIZE_UM:.2f} um"
)


def _equipment_exif_args() -> list:
    return [
        f"-Make={CAMERA_MAKE}",
        f"-Model={CAMERA_MODEL}",
        f"-LensMake={LENS_MAKE}",
        f"-LensModel={LENS_MODEL}",
        f"-FocalLength={FOCAL_LENGTH_MM:.1f} mm",
        f"-FocalPlaneXResolution={FOCAL_PLANE_RESOLUTION_PPI:.6f}",
        f"-FocalPlaneYResolution={FOCAL_PLANE_RESOLUTION_PPI:.6f}",
        "-FocalPlaneResolutionUnit=inches",
        f"-UserComment={EXIF_EQUIPMENT_COMMENT}",
    ]


def _exiftool_available() -> bool:
    """检查系统中是否安装了 ExifTool"""
    return shutil.which("exiftool") is not None


def _wait_for_file_ready(
    photo_path: Path,
    timeout_s: float = 10.0,
    interval_s: float = 0.2
) -> bool:
    """等待照片文件完成落盘，避免 watchdog 在 JPEG 尚未写完时触发 EXIF 写入。"""
    deadline = time.monotonic() + timeout_s
    last_size = -1
    stable_count = 0

    while time.monotonic() < deadline:
        try:
            if not photo_path.exists() or not photo_path.is_file():
                stable_count = 0
                time.sleep(interval_s)
                continue

            size = photo_path.stat().st_size
            if size <= 0:
                stable_count = 0
                time.sleep(interval_s)
                continue

            with photo_path.open("rb"):
                pass

            if size == last_size:
                stable_count += 1
                if stable_count >= 2:
                    return True
            else:
                stable_count = 0
                last_size = size

        except OSError as e:
            logger.debug(f"等待照片文件可读: {photo_path}, {e}")

        time.sleep(interval_s)

    logger.error(f"等待照片文件落盘超时，跳过 EXIF 写入: {photo_path}")
    return False


def modify_photo_equipment_exif(photo_path: Path) -> bool:
    """只写入固定相机/镜头 EXIF；用于惯导暂时无数据时保留设备元数据。"""
    if not _exiftool_available():
        logger.error("系统中未找到 ExifTool，无法写入 EXIF。请确保 exiftool 已在 PATH 中。")
        return False

    if not _wait_for_file_ready(photo_path):
        return False

    cmd = [
        "exiftool",
        "-overwrite_original",
        *_equipment_exif_args(),
        str(photo_path),
    ]

    for exif_attempt in range(1, 4):
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
                logger.debug(f"成功写入相机/镜头 EXIF 属性: {photo_path}")
                return True

            logger.error(
                f"写入相机/镜头 EXIF 属性失败 "
                f"(exiftool 返回 {result.returncode}, 尝试 {exif_attempt}/3): "
                f"{result.stderr.strip()}"
            )
        except Exception as e:
            logger.error(f"调用 ExifTool 异常 (尝试 {exif_attempt}/3): {e}")

        time.sleep(0.5)

    return False


def modify_photo_exif(
    photo_path: Path,
    gps_data: Optional[GpsExifData] = None,
    max_gps_age_s: Optional[float] = None
) -> bool:
    """
    使用 ExifTool 修改照片 EXIF 属性

    Args:
        photo_path: 照片文件的完整路径
        gps_data: 指定 GPS/高度/UTC 快照。传入时会直接使用该快照，不再读取最新缓存。
        max_gps_age_s: 未传入 gps_data 时，允许读取的传感器缓存最大年龄。

    Returns:
        bool: 是否成功
    """
    if not _exiftool_available():
        logger.error("系统中未找到 ExifTool，无法写入 EXIF。请确保 exiftool 已在 PATH 中。")
        return False

    if not _wait_for_file_ready(photo_path):
        return False

    max_retries = 20
    retry_delay_ms = 500

    for attempt in range(1, max_retries + 1):
        current_gps_data = gps_data
        if current_gps_data is None:
            current_gps_data = sensor_data.get_gps_height_utc(max_age_s=max_gps_age_s)

        if current_gps_data is None:
            logger.error(f"无法获取 GPS 和 UTC 数据 (尝试 {attempt}/{max_retries})，等待 {retry_delay_ms}ms 后重试")
            time.sleep(retry_delay_ms / 1000.0)
            continue

        (
            latitude, longitude, height,
            utc_year, utc_month, utc_day,
            utc_hour, utc_minute, utc_second
        ) = current_gps_data

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
            *_equipment_exif_args(),
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

        for exif_attempt in range(1, 4):
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

                logger.error(
                    f"修改照片 EXIF 属性失败 "
                    f"(exiftool 返回 {result.returncode}, 尝试 {exif_attempt}/3): "
                    f"{result.stderr.strip()}"
                )
            except Exception as e:
                logger.error(f"调用 ExifTool 异常 (尝试 {exif_attempt}/3): {e}")

            time.sleep(retry_delay_ms / 1000.0)

        return False

    logger.error(f"获取 GPS 和 UTC 数据失败，已达到最大重试次数，跳过照片: {photo_path}")
    return False
