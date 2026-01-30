#-*- coding: utf-8 -*-
"""
协议帧构建模块

帧结构:
┌────────┬────────┬────────┬────────┬────────┬─────────┬────────┬────────┐
│ 帧头1  │ 帧头2  │ 版本号 │ 长度   │ 命令码 │ 数据段  │ 校验   │ 帧尾   │
│ 0xFE   │ 0xFE   │ 0x20   │ 4字节  │ 1字节  │ N字节   │ 1字节  │ 0xEFEF │
└────────┴────────┴────────┴────────┴────────┴─────────┴────────┴────────┘

- 帧头: FE FE
- 版本号: 0x20 (v2.0)
- 长度: 4字节大端序（命令码+数据段长度）
- 命令码: 1字节
- 数据段: N字节
- 校验: XOR（版本号+长度+命令码+数据段）
- 帧尾: EF EF
"""

import struct
from typing import Optional, Tuple
from loguru import logger


#协议常量
FRAME_HEADER = b'\xFE\xFE'
FRAME_FOOTER = b'\xEF\xEF'
PROTOCOL_VERSION = 0x20  #v2.0


#命令码定义 - 控制命令（上位机 → 客户端）
class Command:
    """命令码常量"""
    #控制命令
    CAPTURE = 0x10          #单次拍照
    RECORD_START = 0x11     #开始录像
    RECORD_STOP = 0x12      #停止录像
    PREVIEW_START = 0x13    #开启实时预览
    PREVIEW_STOP = 0x14     #停止实时预览
    CONTINUOUS_START = 0x15 #开始连续拍照
    CONTINUOUS_STOP = 0x16  #停止连续拍照
    SET_EXPOSURE = 0x20     #设置曝光
    SET_WHITE_BALANCE = 0x21  #设置白平衡
    SET_GAIN = 0x22         #设置增益
    SET_RESOLUTION = 0x23   #设置分辨率
    SET_GAIN_AUTO = 0x24    #设置自动增益
    SET_FRAME_RATE = 0x25   #设置帧率
    SET_PIXEL_FORMAT = 0x26 #设置像素格式
    QUERY_STATUS = 0x30     #查询状态
    QUERY_PARAMS = 0x31     #查询参数
    QUERY_RESOLUTIONS = 0x32  #查询支持的分辨率列表
    QUERY_GAIN_AUTO = 0x33  #查询自动增益状态
    HEARTBEAT = 0xFF        #心跳

    #应答/状态（客户端 → 上位机）
    ACK_SUCCESS = 0x90      #操作成功
    ACK_FAILED = 0x91       #操作失败
    STATUS_REPORT = 0xA0    #状态上报
    PARAMS_REPORT = 0xA1    #参数上报
    RESOLUTIONS_REPORT = 0xA2  #分辨率列表上报
    GAIN_AUTO_REPORT = 0xA3 #自动增益状态上报
    CAPTURE_DONE = 0xB0     #拍照完成
    RECORD_DONE = 0xB1      #录像完成
    PREVIEW_FRAME = 0xC0    #预览帧数据


def calculate_xor(data: bytes) -> int:
    """
    计算XOR校验值

    Args:
        data: 需要校验的数据（版本号+长度+命令码+数据段）

    Returns:
        XOR校验值（1字节）
    """
    result = 0
    for byte in data:
        result ^= byte
    return result


def build_frame(cmd: int, data: bytes = b'', version: int = PROTOCOL_VERSION) -> bytes:
    """
    构建协议帧

    Args:
        cmd: 命令码
        data: 数据段（可为空）
        version: 协议版本号，默认0x20

    Returns:
        完整的协议帧字节串
    """
    #计算长度（命令码+数据段）
    length = 1 + len(data)

    #构建需要校验的部分：版本号+长度(4字节大端)+命令码+数据段
    payload = bytes([version]) + struct.pack('>I', length) + bytes([cmd]) + data

    #计算XOR校验
    xor_byte = calculate_xor(payload)

    #组装完整帧
    frame = FRAME_HEADER + payload + bytes([xor_byte]) + FRAME_FOOTER

    logger.debug(f"构建帧: cmd=0x{cmd:02X}, data_len={len(data)}, frame={frame.hex().upper()}")
    return frame


def parse_frame(data: bytes) -> Optional[Tuple[int, int, bytes]]:
    """
    解析协议帧

    Args:
        data: 完整的协议帧数据

    Returns:
        成功返回 (版本号, 命令码, 数据段)，失败返回 None
    """
    #最小帧长度：帧头(2)+版本(1)+长度(4)+命令(1)+校验(1)+帧尾(2) = 11
    if len(data) < 11:
        logger.warning(f"帧长度不足: {len(data)} < 11")
        return None

    #检查帧头
    if data[:2] != FRAME_HEADER:
        logger.warning(f"帧头错误: {data[:2].hex().upper()}")
        return None

    #检查帧尾
    if data[-2:] != FRAME_FOOTER:
        logger.warning(f"帧尾错误: {data[-2:].hex().upper()}")
        return None

    #解析版本号
    version = data[2]

    #解析长度（大端序，4字节）
    length = struct.unpack('>I', data[3:7])[0]

    #检查帧完整性
    expected_len = 2 + 1 + 4 + length + 1 + 2  #帧头+版本+长度(4)+数据+校验+帧尾
    if len(data) != expected_len:
        logger.warning(f"帧长度不匹配: 期望{expected_len}, 实际{len(data)}")
        return None

    #提取命令码和数据段
    cmd = data[7]
    payload_data = data[8:-3] if length > 1 else b''

    #验证XOR校验
    payload = data[2:-3]  #版本号+长度+命令码+数据段
    expected_xor = data[-3]
    actual_xor = calculate_xor(payload)

    if expected_xor != actual_xor:
        logger.warning(f"XOR校验失败: 期望0x{expected_xor:02X}, 实际0x{actual_xor:02X}")
        return None

    logger.debug(f"解析帧成功: version=0x{version:02X}, cmd=0x{cmd:02X}, data_len={len(payload_data)}")
    return (version, cmd, payload_data)


#命令封装函数
def build_heartbeat() -> bytes:
    """构建心跳命令帧"""
    return build_frame(Command.HEARTBEAT)


def build_capture() -> bytes:
    """构建单次拍照命令帧"""
    return build_frame(Command.CAPTURE)


def build_continuous_start() -> bytes:
    """构建开始连续拍照命令帧"""
    return build_frame(Command.CONTINUOUS_START)


def build_continuous_stop() -> bytes:
    """构建停止连续拍照命令帧"""
    return build_frame(Command.CONTINUOUS_STOP)


def build_record_start(duration: int = 0, resolution_index: int = 0, fps: int = 5) -> bytes:
    """
    构建开始录像命令帧

    Args:
        duration: 录像时长（秒），0表示手动停止
        resolution_index: 分辨率索引
        fps: 帧率

    Returns:
        命令帧
    """
    data = struct.pack('>I', duration) + bytes([resolution_index, fps])
    return build_frame(Command.RECORD_START, data)


def build_record_stop() -> bytes:
    """构建停止录像命令帧"""
    return build_frame(Command.RECORD_STOP)


def build_preview_start(resolution_index: int = 0, fps: int = 10) -> bytes:
    """
    构建开启预览命令帧

    Args:
        resolution_index: 分辨率索引
        fps: 帧率

    Returns:
        命令帧
    """
    data = bytes([resolution_index, fps])
    return build_frame(Command.PREVIEW_START, data)


def build_preview_stop() -> bytes:
    """构建停止预览命令帧"""
    return build_frame(Command.PREVIEW_STOP)


def build_set_exposure(mode: int, value: int) -> bytes:
    """
    构建设置曝光命令帧

    Args:
        mode: 模式（0-自动, 1-手动）
        value: 曝光值（微秒）

    Returns:
        命令帧
    """
    data = bytes([mode]) + struct.pack('>I', value)
    return build_frame(Command.SET_EXPOSURE, data)


def build_set_white_balance(mode: int, r: int = 0, g: int = 0, b: int = 0) -> bytes:
    """
    构建设置白平衡命令帧

    Args:
        mode: 模式（0-自动, 1-手动）
        r: 红色增益
        g: 绿色增益
        b: 蓝色增益

    Returns:
        命令帧
    """
    data = bytes([mode]) + struct.pack('>HHH', r, g, b)
    return build_frame(Command.SET_WHITE_BALANCE, data)


def build_set_gain(value: int) -> bytes:
    """
    构建设置增益命令帧

    Args:
        value: 增益值（0-1000）

    Returns:
        命令帧
    """
    data = struct.pack('>H', value)
    return build_frame(Command.SET_GAIN, data)


def build_set_resolution(width: int, height: int) -> bytes:
    """
    构建设置分辨率命令帧

    Args:
        width: 宽度
        height: 高度

    Returns:
        命令帧
    """
    data = struct.pack('>HH', width, height)
    return build_frame(Command.SET_RESOLUTION, data)


def build_query_status() -> bytes:
    """构建查询状态命令帧"""
    return build_frame(Command.QUERY_STATUS)


def build_query_params() -> bytes:
    """构建查询参数命令帧"""
    return build_frame(Command.QUERY_PARAMS)


def build_query_resolutions() -> bytes:
    """构建查询分辨率列表命令帧"""
    return build_frame(Command.QUERY_RESOLUTIONS)


def build_set_gain_auto(mode: int) -> bytes:
    """
    构建设置自动增益命令帧

    Args:
        mode: 模式（0=关闭, 1=开启）

    Returns:
        命令帧
    """
    data = bytes([mode])
    return build_frame(Command.SET_GAIN_AUTO, data)


def build_set_frame_rate(fps: int, enable: bool = True) -> bytes:
    """
    构建设置帧率命令帧

    Args:
        fps: 帧率值（实际帧率*100，如30.5fps传入3050）
        enable: 是否启用帧率限制

    Returns:
        命令帧
    """
    #帧率使用4字节大端序存储（帧率*100的整数值）
    data = bytes([1 if enable else 0]) + struct.pack('>I', fps)
    return build_frame(Command.SET_FRAME_RATE, data)


def build_set_pixel_format(format_index: int) -> bytes:
    """
    构建设置像素格式命令帧

    Args:
        format_index: 像素格式索引

    Returns:
        命令帧
    """
    data = bytes([format_index])
    return build_frame(Command.SET_PIXEL_FORMAT, data)


def build_query_gain_auto() -> bytes:
    """构建查询自动增益状态命令帧"""
    return build_frame(Command.QUERY_GAIN_AUTO)


#响应解析函数
def parse_ack_success(data: bytes) -> Optional[int]:
    """
    解析操作成功响应

    Args:
        data: 数据段

    Returns:
        原命令码，失败返回None
    """
    if len(data) < 1:
        return None
    return data[0]


def parse_ack_failed(data: bytes) -> Optional[Tuple[int, int]]:
    """
    解析操作失败响应

    Args:
        data: 数据段

    Returns:
        (原命令码, 错误码)，失败返回None
    """
    if len(data) < 3:
        return None
    cmd = data[0]
    error_code = struct.unpack('>H', data[1:3])[0]
    return (cmd, error_code)


def parse_preview_frame(data: bytes) -> Optional[Tuple[int, bytes]]:
    """
    解析预览帧数据

    Args:
        data: 数据段

    Returns:
        (帧序号, JPEG数据)，失败返回None
    """
    if len(data) < 8:
        return None
    seq = struct.unpack('>I', data[:4])[0]
    jpeg_len = struct.unpack('>I', data[4:8])[0]
    if len(data) < 8 + jpeg_len:
        return None
    jpeg_data = data[8:8+jpeg_len]
    return (seq, jpeg_data)


def parse_gain_auto_report(data: bytes) -> Optional[bool]:
    """
    解析自动增益状态响应

    Args:
        data: 数据段

    Returns:
        自动增益是否启用，失败返回None
    """
    if len(data) < 1:
        return None
    return data[0] == 1


if __name__ == '__main__':
    #测试代码
    import sys
    logger.remove()
    logger.add(sys.stdout, level="DEBUG")

    #测试心跳帧
    heartbeat = build_heartbeat()
    print(f"心跳帧: {heartbeat.hex().upper()}")

    #测试解析
    result = parse_frame(heartbeat)
    if result:
        version, cmd, data = result
        print(f"解析结果: version=0x{version:02X}, cmd=0x{cmd:02X}, data={data.hex()}")

    #测试拍照帧
    capture = build_capture()
    print(f"拍照帧: {capture.hex().upper()}")

    #测试开始录像帧
    record = build_record_start(duration=60, resolution_index=1, fps=5)
    print(f"录像帧: {record.hex().upper()}")

    #测试预览帧
    preview = build_preview_start(resolution_index=0, fps=10)
    print(f"预览帧: {preview.hex().upper()}")
