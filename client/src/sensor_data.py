# -*- coding: utf-8 -*-
"""
传感器数据接收模块

负责：
- 通过 TCP 连接 RELAY 数据源（192.168.1.201:2000）
- 解析 110 字节和 134 字节数据帧
- 线程安全缓存 GPS、高度、UTC 时间等关键字段
- 提供 get_gps_height_utc() 接口供 EXIF 写入模块使用

性能与可靠性：
- 独立后台线程运行
- 连接断开后 5 秒自动重连
- 使用 threading.Lock 保护共享数据
"""

import socket
import struct
import threading
import time
from collections import deque
from typing import Optional, Dict, Tuple

from loguru import logger

# 默认数据源地址和端口
DEFAULT_RELAY_ADDRESS = ("192.168.1.119", 8081)
DATA_IDLE_RECONNECT_SEC = 2.0
RECONNECT_DELAY_SEC = 1.0

# 全局解析结果缓存
_result: Dict[str, str] = {}
_result_updated_at = 0.0
_result_history = deque(maxlen=300)
_result_lock = threading.Lock()

# 线程控制标志
_should_exit = threading.Event()
_receiver_thread: Optional[threading.Thread] = None


class NavDataBuffer:
    """134 字节导航数据帧滑动窗口缓存"""
    def __init__(self):
        self.header = 0xFA
        self.length = 133
        self.end = bytes([0xFB, 0xBF])
        self.location = 0
        self.data = bytearray(133)


NAV_134_HEADER = b"\xFA\xAF"
NAV_134_FOOTER = b"\xFB\xBF"
NAV_134_FRAME_LEN = 133


def _xor_checksum(data: bytes) -> int:
    if not data:
        return 0

    checksum = data[0]
    for value in data[1:]:
        checksum ^= value
    return checksum & 0xFF


def _extract_gps_height_utc(result: Dict[str, str]) -> Optional[Tuple[str, str, str, str, str, str, str, str, str]]:
    try:
        latitude = result["latitude"]
        longitude = result["longitude"]
        height = result["height"]
        utc_year = result["utc_year"]
        utc_month = result["utc_month"]
        utc_day = result["utc_day"]
        utc_hour = result["utc_hour"]
        utc_minute = result["utc_minute"]
        utc_second = result["utc_second"]
        return (
            latitude, longitude, height,
            utc_year, utc_month, utc_day,
            utc_hour, utc_minute, utc_second
        )
    except KeyError:
        return None


def _cache_result(parsed: Dict[str, str], received_timestamp: Optional[float] = None) -> None:
    """缓存最新惯导解析结果，并保留本机接收时间用于按拍照时刻匹配。"""
    global _result_updated_at
    if not parsed:
        return

    timestamp = received_timestamp if received_timestamp is not None else time.monotonic()
    snapshot = parsed.copy()
    with _result_lock:
        _result.clear()
        _result.update(snapshot)
        _result_updated_at = timestamp
        if _extract_gps_height_utc(snapshot) is not None:
            _result_history.append((timestamp, snapshot))


def _parse_110_bytes(data: bytes) -> Dict[str, str]:
    """解析 110 字节数据帧"""
    result = {}
    if len(data) != 110:
        return result

    # 报文头（0-1 字节）
    result["header"] = data[:2].hex().upper()

    # 模式（2 字节）
    result["mode"] = f"{data[2]:02X}"

    # 温度（3-4 字节，大端序）
    temp = struct.unpack(">H", data[3:5])[0] / 100.0
    result["temperature"] = str(temp)

    # 控制电压（5-6 字节）
    ctrl_volt = struct.unpack(">H", data[5:7])[0]
    result["control_voltage"] = str(ctrl_volt)

    # 电源电压（7-8 字节）
    power_volt = struct.unpack(">H", data[7:9])[0]
    result["power_voltage"] = str(power_volt)

    # 泄漏报警（9 字节）
    result["leak_alarm"] = "是" if data[9] == 1 else "否"

    # 传感器有效（10 字节，8 位二进制反转）
    result["sensor_valid"] = f"{data[10]:08b}"[::-1]

    # 传感器更新（11 字节，8 位二进制反转）
    result["sensor_updated"] = f"{data[11]:08b}"[::-1]

    # 故障状态（12-13 字节，16 位二进制反转）
    fault_status = struct.unpack(">H", data[12:14])[0]
    result["fault_status"] = f"{fault_status:016b}"[::-1]

    # 电源状态（14-15 字节）
    result["power_status"] = str(struct.unpack(">H", data[14:16])[0])

    # 力值 TX, TY, TZ, MX, MY, MZ（16-27 字节）
    names = ["TX", "TY", "TZ", "MX", "MY", "MZ"]
    for i, name in enumerate(names):
        offset = 16 + i * 2
        val = struct.unpack(">h", data[offset:offset + 2])[0]
        result[f"{name}_force"] = str(val)

    # 姿态角：横滚、俯仰、航向（28-33 字节，大端序，单位 0.01°）
    roll = struct.unpack(">h", data[28:30])[0] / 100.0
    pitch = struct.unpack(">h", data[30:32])[0] / 100.0
    yaw = struct.unpack(">h", data[32:34])[0] / 100.0
    result["roll"] = str(roll)
    result["pitch"] = str(pitch)
    result["yaw"] = str(yaw)

    # 陀螺仪值（34-39 字节）
    axes = ["R", "P", "Y"]
    offsets = [34, 36, 38]
    for axis, offset in zip(axes, offsets):
        gyro_val = struct.unpack(">h", data[offset:offset + 2])[0] / 100.0
        result[f"{axis}_gyro"] = str(gyro_val)

    # 速度（40-45 字节）
    vel_u = struct.unpack(">h", data[40:42])[0] / 100.0
    vel_v = struct.unpack(">h", data[42:44])[0] / 100.0
    vel_w = struct.unpack(">h", data[44:46])[0] / 100.0
    result["velocity_u"] = str(vel_u)
    result["velocity_v"] = str(vel_v)
    result["velocity_w"] = str(vel_w)

    # GPS 坐标（46-53 字节，小端序）
    longitude = struct.unpack("<I", data[46:50])[0] / 1e7
    latitude = struct.unpack("<I", data[50:54])[0] / 1e7
    result["longitude"] = str(longitude)
    result["latitude"] = str(latitude)
    logger.debug(f"GPS 坐标: ({latitude}, {longitude})")

    # 深度和高度（54-65 字节，小端序 f32）
    depth = struct.unpack("<f", data[54:58])[0]
    height = struct.unpack("<f", data[58:62])[0]
    obstacle = struct.unpack("<f", data[62:66])[0]
    result["depth"] = str(depth)
    result["height"] = str(height)
    result["obstacle"] = str(obstacle)

    # 目标坐标（66-77 字节）
    target_lon = struct.unpack("<I", data[66:70])[0] / 1e7
    target_lat = struct.unpack("<I", data[70:74])[0] / 1e7
    target_depth = struct.unpack("<f", data[74:78])[0]
    result["target_longitude"] = str(target_lon)
    result["target_latitude"] = str(target_lat)
    result["target_depth"] = str(target_depth)

    # 目标姿态角（78-83 字节）
    target_roll = struct.unpack(">h", data[78:80])[0] / 100.0
    target_pitch = struct.unpack(">h", data[80:82])[0] / 100.0
    target_yaw = struct.unpack(">h", data[82:84])[0] / 100.0
    result["target_roll"] = str(target_roll)
    result["target_pitch"] = str(target_pitch)
    result["target_yaw"] = str(target_yaw)

    # 目标高度和速度（84-89 字节）
    target_height = struct.unpack("<f", data[84:88])[0]
    target_speed = struct.unpack(">H", data[88:90])[0]
    result["target_height"] = str(target_height)
    result["target_speed"] = str(target_speed)

    # UTC 时间（90-98 字节）
    result["utc_year"] = str(data[90])
    result["utc_month"] = str(data[91])
    result["utc_day"] = str(data[92])
    result["utc_hour"] = str(data[93])
    result["utc_minute"] = str(data[94])
    utc_second = struct.unpack("<f", data[95:99])[0]
    result["utc_second"] = str(utc_second)
    logger.debug(
        f"UTC 时间: {data[90]}年{data[91]}月{data[92]}日 "
        f"{data[93]}时{data[94]}分{utc_second:.2f}秒"
    )

    # 校验和与报文尾（107-109 字节）
    result["checksum"] = str(data[107])
    result["checksum_ok"] = "true"
    result["footer"] = data[108:110].hex().upper()

    return result


def _parse_134_bytes(data: bytes) -> Dict[str, str]:
    """解析 134 字节数据帧（实际数据体 133 字节）。"""
    result = {}
    if len(data) != 133:
        return result

    # 姿态角（2-13 字节，小端 f32，单位：度）
    roll = struct.unpack("<f", data[2:6])[0]
    pitch = struct.unpack("<f", data[6:10])[0]
    yaw = struct.unpack("<f", data[10:14])[0]
    result["roll"] = str(roll)
    result["pitch"] = str(pitch)
    result["yaw"] = str(yaw)

    # 机体角速度（14-25 字节，小端 f32）
    result["body_angular_velocity_roll"] = str(struct.unpack("<f", data[14:18])[0])
    result["body_angular_velocity_pitch"] = str(struct.unpack("<f", data[18:22])[0])
    result["body_angular_velocity_yaw"] = str(struct.unpack("<f", data[22:26])[0])

    # 机体速度（26-37 字节，小端 f32）：前、右、下
    result["body_velocity_forward"] = str(struct.unpack("<f", data[26:30])[0])
    result["body_velocity_right"] = str(struct.unpack("<f", data[30:34])[0])
    result["body_velocity_down"] = str(struct.unpack("<f", data[34:38])[0])

    # GPS 坐标（38-45 字节，小端 uint32，单位：度 * 1e7）
    latitude = struct.unpack("<I", data[38:42])[0] / 1e7
    longitude = struct.unpack("<I", data[42:46])[0] / 1e7
    result["longitude"] = str(longitude)
    result["latitude"] = str(latitude)

    # 压力深度和对海底高度（107-114 字节，小端 f32）
    # depth: 距海面深度，>=0，越深越大；height 用于 EXIF，按海平面高度约定写成负深度。
    depth = struct.unpack("<f", data[107:111])[0]
    altitude = struct.unpack("<f", data[111:115])[0]
    result["depth"] = str(depth)
    result["height"] = str(-depth)
    result["altitude"] = str(altitude)
    result["height_above_bottom"] = str(altitude)

    # 状态字节（115、129 字节）
    sensor_valid = data[115]
    result["sensor_valid"] = f"{sensor_valid:08b}"
    result["sensor_valid_raw"] = str(sensor_valid)
    result["ins_state"] = str(data[129])

    dvl_state = 0
    gps_state = 0
    if sensor_valid & 0x01:
        dvl_state = 1
    if sensor_valid & (0x01 << 1):
        dvl_state = 2
    if sensor_valid & (0x01 << 2):
        gps_state = 1
    if sensor_valid & (0x01 << 3):
        gps_state = 2
    result["dvl_state"] = str(dvl_state)
    result["gps_state"] = str(gps_state)

    # UTC 时间（116-124 字节）
    result["utc_year"] = str(data[116])
    result["utc_month"] = str(data[117])
    result["utc_day"] = str(data[118])
    result["utc_hour"] = str(data[119])
    result["utc_minute"] = str(data[120])
    utc_second = struct.unpack("<f", data[121:125])[0]
    result["utc_second"] = str(utc_second)
    logger.debug(
        f"UTC 时间: {data[116]}年{data[117]}月{data[118]}日 "
        f"{data[119]}时{data[120]}分{utc_second:.2f}秒"
    )
    logger.debug(
        f"惯导姿态: roll={roll:.3f}, pitch={pitch:.3f}, yaw={yaw:.3f}; "
        f"GPS=({latitude}, {longitude}); depth={depth:.3f}m; "
        f"altitude={altitude:.3f}m; ins={data[129]}, gps={gps_state}, dvl={dvl_state}"
    )

    result["checksum"] = str(data[130])
    result["checksum_ok"] = "true"
    result["footer"] = data[131:133].hex().upper()

    return result


def _read_110_data(sock: socket.socket) -> None:
    """读取并解析 110 字节定长数据"""
    last_data_at = time.monotonic()
    while not _should_exit.is_set():
        try:
            sock.settimeout(1.0)
            data = b""
            while len(data) < 110 and not _should_exit.is_set():
                chunk = sock.recv(110 - len(data))
                if not chunk:
                    raise ConnectionError("连接已关闭")
                data += chunk
                last_data_at = time.monotonic()
            if len(data) == 110:
                parsed = _parse_110_bytes(data)
                _cache_result(parsed, received_timestamp=last_data_at)
                logger.debug(f"解析结果: {_result}")
        except socket.timeout:
            if time.monotonic() - last_data_at > DATA_IDLE_RECONNECT_SEC:
                logger.warning(
                    f"110 数据超过 {DATA_IDLE_RECONNECT_SEC:.1f}s 未更新，主动重连传感器数据源"
                )
                break
            continue
        except Exception as e:
            logger.error(f"读取 110 数据错误: {e}")
            break


def _read_134_data(sock: socket.socket) -> None:
    """读取并解析 134 字节滑动窗口数据"""
    stream = bytearray()
    last_valid_frame_at = time.monotonic()

    while not _should_exit.is_set():
        try:
            sock.settimeout(1.0)
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("连接已关闭")
            received_at = time.monotonic()
            stream.extend(chunk)

            while not _should_exit.is_set():
                header_index = stream.find(NAV_134_HEADER)
                if header_index < 0:
                    if len(stream) > 1:
                        # 保留最后 1 字节，避免帧头 FA/AF 被分在两次 recv 中。
                        del stream[:-1]
                    break

                if header_index > 0:
                    del stream[:header_index]

                if len(stream) < NAV_134_FRAME_LEN:
                    break

                frame = bytes(stream[:NAV_134_FRAME_LEN])
                if frame[-2:] != NAV_134_FOOTER:
                    logger.debug("134 数据帧尾校验失败，继续滑动查找下一帧头")
                    del stream[0]
                    continue

                checksum_calc = _xor_checksum(frame[:130])
                if frame[130] != checksum_calc:
                    logger.warning(
                        f"134 数据校验失败: recv=0x{frame[130]:02X}, calc=0x{checksum_calc:02X}"
                    )
                    del stream[0]
                    continue

                logger.debug(f"解析到有效数据: {frame[:20].hex().upper()}...")
                parsed = _parse_134_bytes(frame)
                _cache_result(parsed, received_timestamp=received_at)
                last_valid_frame_at = received_at
                del stream[:NAV_134_FRAME_LEN]

            if time.monotonic() - last_valid_frame_at > DATA_IDLE_RECONNECT_SEC:
                logger.warning(
                    f"134 有效数据超过 {DATA_IDLE_RECONNECT_SEC:.1f}s 未更新，主动重连传感器数据源"
                )
                break

        except socket.timeout:
            if time.monotonic() - last_valid_frame_at > DATA_IDLE_RECONNECT_SEC:
                logger.warning(
                    f"134 有效数据超过 {DATA_IDLE_RECONNECT_SEC:.1f}s 未更新，主动重连传感器数据源"
                )
                break
            continue
        except Exception as e:
            logger.error(f"读取 134 数据错误: {e}")
            break


def _tcp_connect_loop(address: Tuple[str, int], mode: str) -> None:
    """TCP 连接循环，支持自动重连"""
    while not _should_exit.is_set():
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect(address)
            logger.info(f"成功连接到传感器数据源: {address[0]}:{address[1]}")

            if mode == "110":
                _read_110_data(sock)
            elif mode == "134":
                _read_134_data(sock)
            else:
                logger.error(f"未知解析模式: {mode}")
                break

        except Exception as e:
            logger.error(f"连接传感器数据源失败 {address[0]}:{address[1]}: {e}")

        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

        if not _should_exit.is_set():
            logger.info(f"{RECONNECT_DELAY_SEC:.1f} 秒后重连传感器数据源...")
            for _ in range(max(1, int(RECONNECT_DELAY_SEC * 10))):
                if _should_exit.is_set():
                    break
                time.sleep(0.1)

    logger.debug("传感器数据接收线程正在退出...")


def start_sensor_data_receiver(
    address: Tuple[str, int] = DEFAULT_RELAY_ADDRESS,
    mode: str = "110"
) -> None:
    """
    启动传感器数据接收线程

    Args:
        address: 数据源地址和端口，默认为 ("192.168.1.201", 2000)
        mode: 解析模式，"110" 或 "134"
    """
    global _receiver_thread

    if _receiver_thread is not None and _receiver_thread.is_alive():
        logger.warning("传感器数据接收线程已在运行")
        return

    _should_exit.clear()
    _receiver_thread = threading.Thread(
        target=_tcp_connect_loop,
        args=(address, mode),
        daemon=True,
        name="SensorDataReceiver"
    )
    _receiver_thread.start()
    logger.info(f"启动传感器数据接收线程: {address[0]}:{address[1]}, 模式={mode}")


def stop_sensor_data_receiver() -> None:
    """停止传感器数据接收线程"""
    global _receiver_thread
    _should_exit.set()
    if _receiver_thread is not None:
        _receiver_thread.join(timeout=3.0)
        _receiver_thread = None
    logger.info("传感器数据接收线程已停止")


def get_gps_height_utc(max_age_s: Optional[float] = None) -> Optional[Tuple[str, str, str, str, str, str, str, str, str]]:
    """
    获取 GPS、高度与 UTC 时间

    Args:
        max_age_s: 数据最大允许年龄（秒）。None 表示不检查新鲜度。

    Returns:
        Optional[Tuple]: (纬度, 经度, 高度, 年, 月, 日, 时, 分, 秒)
                         如果解析结果不存在或缺少必要字段，返回 None
    """
    with _result_lock:
        result = _result.copy()
        updated_at = _result_updated_at

    if max_age_s is not None:
        if updated_at <= 0:
            return None
        age_s = time.monotonic() - updated_at
        if age_s > max_age_s:
            logger.warning(f"惯导数据已过期: {age_s:.2f}s > {max_age_s:.2f}s")
            return None

    return _extract_gps_height_utc(result)


def get_gps_height_utc_nearest(
    target_timestamp: float,
    max_delta_s: Optional[float] = None
) -> Optional[Tuple[str, str, str, str, str, str, str, str, str]]:
    """
    获取最接近指定本机单调时钟时刻的 GPS、高度与 UTC 时间。

    Args:
        target_timestamp: 使用 time.monotonic() 记录的目标时刻，通常为相机抓图触发时刻。
        max_delta_s: 最大允许时间差（秒）。None 表示不检查。

    Returns:
        Optional[Tuple]: (纬度, 经度, 高度, 年, 月, 日, 时, 分, 秒)
                         如果历史为空、字段缺失或超过最大时间差，返回 None。
    """
    nav_data = get_nav_data_nearest(target_timestamp, max_delta_s=max_delta_s)
    if nav_data is None:
        return None

    return _extract_gps_height_utc(nav_data)


def get_nav_data_nearest(
    target_timestamp: float,
    max_delta_s: Optional[float] = None
) -> Optional[Dict[str, str]]:
    """
    获取最接近指定本机单调时钟时刻的完整惯导快照。

    本机时间只用于选择哪一帧惯导数据；返回的快照不包含本机时间，
    写入照片时应使用快照中的 GPS、深度、UTC、姿态等实际惯导字段。
    """
    with _result_lock:
        history = list(_result_history)

    if not history:
        return None

    best_timestamp, best_result = min(
        history,
        key=lambda item: abs(item[0] - target_timestamp)
    )
    delta_s = abs(best_timestamp - target_timestamp)

    if max_delta_s is not None and delta_s > max_delta_s:
        logger.warning(
            f"未找到足够接近拍照时刻的惯导数据: "
            f"delta={delta_s:.3f}s > {max_delta_s:.3f}s"
        )
        return None

    if _extract_gps_height_utc(best_result) is None:
        return None

    logger.debug(f"匹配拍照时刻惯导数据: delta={delta_s:.3f}s")
    return best_result.copy()


def describe_gps_history(target_timestamp: Optional[float] = None) -> str:
    """返回当前惯导历史缓存状态，供拍照 EXIF 失败日志诊断。"""
    now = time.monotonic()
    with _result_lock:
        history = list(_result_history)
        updated_at = _result_updated_at

    if not history:
        if updated_at > 0:
            return f"history=0, latest_result_age={now - updated_at:.3f}s, latest_result_has_no_gps"
        return "history=0, no_parsed_result"

    latest_timestamp, latest_result = history[-1]
    latest_age_s = now - latest_timestamp
    gps_data = _extract_gps_height_utc(latest_result)

    detail = f"history={len(history)}, latest_age={latest_age_s:.3f}s"
    if target_timestamp is not None:
        nearest_timestamp, _ = min(history, key=lambda item: abs(item[0] - target_timestamp))
        detail += f", nearest_delta={abs(nearest_timestamp - target_timestamp):.3f}s"

    if gps_data is not None:
        latitude, longitude, height, year, month, day, hour, minute, second = gps_data
        detail += (
            f", latest_gps=({latitude},{longitude}), height={height}, "
            f"utc={year}-{month}-{day} {hour}:{minute}:{second}"
        )

    return detail
