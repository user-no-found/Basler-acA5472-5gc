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
from typing import Optional, Dict, Tuple

from loguru import logger

# 默认数据源地址和端口
DEFAULT_RELAY_ADDRESS = ("192.168.1.201", 2000)

# 全局解析结果缓存
_result: Dict[str, str] = {}
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
    """解析 134 字节数据帧（实际数据体 133 字节）"""
    result = {}
    if len(data) != 133:
        return result

    # GPS 坐标（83-90 字节，小端序）
    longitude = struct.unpack("<I", data[83:87])[0] / 1e7
    latitude = struct.unpack("<I", data[87:91])[0] / 1e7
    result["longitude"] = str(longitude)
    result["latitude"] = str(latitude)
    logger.debug(f"GPS 坐标: ({latitude}, {longitude})")

    # 深度和高度（91-94 字节，小端序 f32）
    height = struct.unpack("<f", data[91:95])[0]
    result["height"] = str(height)

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

    return result


def _read_110_data(sock: socket.socket) -> None:
    """读取并解析 110 字节定长数据"""
    while not _should_exit.is_set():
        try:
            sock.settimeout(1.0)
            data = b""
            while len(data) < 110 and not _should_exit.is_set():
                chunk = sock.recv(110 - len(data))
                if not chunk:
                    raise ConnectionError("连接已关闭")
                data += chunk
            if len(data) == 110:
                parsed = _parse_110_bytes(data)
                with _result_lock:
                    _result.clear()
                    _result.update(parsed)
                logger.debug(f"解析结果: {_result}")
        except socket.timeout:
            continue
        except Exception as e:
            logger.error(f"读取 110 数据错误: {e}")
            break


def _read_134_data(sock: socket.socket) -> None:
    """读取并解析 134 字节滑动窗口数据"""
    nav = NavDataBuffer()
    buffer = bytearray(1024)

    while not _should_exit.is_set():
        try:
            sock.settimeout(1.0)
            bytes_read = sock.recv_into(buffer)
            if bytes_read == 0:
                raise ConnectionError("连接已关闭")

            bytes_to_copy = bytes_read
            location_header = nav.location

            # 查找帧头
            if nav.location == 0:
                header_index = None
                for i in range(bytes_read):
                    if buffer[i] == nav.header:
                        header_index = i
                        break

                if header_index is not None:
                    nav.location = nav.location + (bytes_to_copy - header_index)
                    if nav.location > nav.length:
                        bytes_to_copy = bytes_to_copy - (nav.location - nav.length)
                        nav.location = nav.length
                    location_end = nav.location
                    start = location_header
                    end = location_end
                    nav.data[start:end] = buffer[header_index:header_index + (end - start)]
            else:
                nav.location = nav.location + bytes_to_copy
                if nav.location > nav.length:
                    bytes_to_copy = bytes_to_copy - (nav.location - nav.length)
                    nav.location = nav.length
                location_end = nav.location
                start = location_header
                end = location_end
                nav.data[start:end] = buffer[:bytes_to_copy]

            # 判断数据是否有效
            if (nav.location == nav.length and
                    nav.data[0] == 0xFA and nav.data[1] == 0xAF and
                    nav.data[131] == 0xFB and nav.data[132] == 0xBF):
                logger.debug(f"解析到有效数据: {nav.data[:20].hex().upper()}...")
                parsed = _parse_134_bytes(nav.data)
                with _result_lock:
                    _result.clear()
                    _result.update(parsed)
                nav.location = 0

        except socket.timeout:
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
            logger.error(f"连接传感器数据源失败: {e}")

        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

        if not _should_exit.is_set():
            logger.info("5 秒后重连传感器数据源...")
            for _ in range(50):
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
    logger.info(f"启动传感器数据接收线程，模式={mode}")


def stop_sensor_data_receiver() -> None:
    """停止传感器数据接收线程"""
    global _receiver_thread
    _should_exit.set()
    if _receiver_thread is not None:
        _receiver_thread.join(timeout=3.0)
        _receiver_thread = None
    logger.info("传感器数据接收线程已停止")


def get_gps_height_utc() -> Optional[Tuple[str, str, str, str, str, str, str, str, str]]:
    """
    获取 GPS、高度与 UTC 时间

    Returns:
        Optional[Tuple]: (纬度, 经度, 高度, 年, 月, 日, 时, 分, 秒)
                         如果解析结果不存在或缺少必要字段，返回 None
    """
    with _result_lock:
        result = _result.copy()

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
