# -*- coding: utf-8 -*-
"""Lever-arm correction from INS center to camera optical center."""

import math
from dataclasses import dataclass
from typing import Dict, Mapping, Optional


EARTH_RADIUS_M = 6378137.0


@dataclass(frozen=True)
class LeverArm:
    """Body-frame lever arm in meters: x forward, y right, z down."""

    forward_m: float
    right_m: float
    down_m: float


DEFAULT_CAMERA_LEVER_ARM = LeverArm(
    forward_m=0.60,
    right_m=0.15,
    down_m=0.10,
)


def _to_float(value: object) -> Optional[float]:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _body_to_ned(
    forward_m: float,
    right_m: float,
    down_m: float,
    roll_deg: float,
    pitch_deg: float,
    yaw_deg: float,
) -> tuple[float, float, float]:
    """Rotate a body-frame vector into NED using a standard Z-Y-X RPY sequence."""

    roll = math.radians(roll_deg)
    pitch = math.radians(pitch_deg)
    yaw = math.radians(yaw_deg)

    sr, cr = math.sin(roll), math.cos(roll)
    sp, cp = math.sin(pitch), math.cos(pitch)
    sy, cy = math.sin(yaw), math.cos(yaw)

    north_m = (
        cy * cp * forward_m
        + (cy * sp * sr - sy * cr) * right_m
        + (cy * sp * cr + sy * sr) * down_m
    )
    east_m = (
        sy * cp * forward_m
        + (sy * sp * sr + cy * cr) * right_m
        + (sy * sp * cr - cy * sr) * down_m
    )
    down_ned_m = (
        -sp * forward_m
        + cp * sr * right_m
        + cp * cr * down_m
    )
    return north_m, east_m, down_ned_m


def apply_camera_lever_arm(
    nav_data: Mapping[str, str],
    lever_arm: LeverArm = DEFAULT_CAMERA_LEVER_ARM,
) -> Dict[str, str]:
    """
    Return a copy of nav_data with latitude/longitude/depth corrected to camera center.

    Input nav_data latitude/longitude/depth are assumed to describe the INS center.
    Output latitude/longitude/depth/height describe the camera optical center.
    The original INS values and correction offsets are preserved in extra keys.
    """

    corrected = dict(nav_data)

    latitude = _to_float(nav_data.get("latitude"))
    longitude = _to_float(nav_data.get("longitude"))
    roll = _to_float(nav_data.get("roll"))
    pitch = _to_float(nav_data.get("pitch"))
    yaw = _to_float(nav_data.get("yaw"))

    if None in (latitude, longitude, roll, pitch, yaw):
        corrected["lever_arm_correction_applied"] = "false"
        corrected["lever_arm_correction_error"] = "missing latitude/longitude/roll/pitch/yaw"
        return corrected

    depth = _to_float(nav_data.get("depth"))
    if depth is None:
        height = _to_float(nav_data.get("height"))
        depth = -height if height is not None else None

    altitude = _to_float(nav_data.get("height_above_bottom"))
    if altitude is None:
        altitude = _to_float(nav_data.get("altitude"))

    north_m, east_m, down_m = _body_to_ned(
        lever_arm.forward_m,
        lever_arm.right_m,
        lever_arm.down_m,
        roll,
        pitch,
        yaw,
    )

    lat_rad = math.radians(latitude)
    cos_lat = max(1e-12, math.cos(lat_rad))
    camera_latitude = latitude + math.degrees(north_m / EARTH_RADIUS_M)
    camera_longitude = longitude + math.degrees(east_m / (EARTH_RADIUS_M * cos_lat))

    corrected["ins_latitude"] = str(latitude)
    corrected["ins_longitude"] = str(longitude)
    corrected["latitude"] = str(camera_latitude)
    corrected["longitude"] = str(camera_longitude)

    if depth is not None:
        camera_depth = depth + down_m
        corrected["ins_depth"] = str(depth)
        corrected["depth"] = str(camera_depth)
        corrected["height"] = str(-camera_depth)

    if altitude is not None:
        camera_altitude = altitude - down_m
        corrected["ins_height_above_bottom"] = str(altitude)
        corrected["altitude"] = str(camera_altitude)
        corrected["height_above_bottom"] = str(camera_altitude)

    corrected["lever_arm_correction_applied"] = "true"
    corrected["lever_arm_forward_m"] = str(lever_arm.forward_m)
    corrected["lever_arm_right_m"] = str(lever_arm.right_m)
    corrected["lever_arm_down_m"] = str(lever_arm.down_m)
    corrected["lever_arm_north_m"] = str(north_m)
    corrected["lever_arm_east_m"] = str(east_m)
    corrected["lever_arm_down_ned_m"] = str(down_m)
    return corrected
