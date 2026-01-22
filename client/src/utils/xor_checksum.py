"""
XOR异或校验工具模块

用于协议帧的校验计算和验证
校验范围：版本号+长度+命令码+数据段
"""


def calculate_xor(data: bytes) -> int:
    """
    计算XOR校验值

    Args:
        data: 需要计算校验的字节数据

    Returns:
        int: 单字节XOR校验值(0-255)
    """
    result = 0
    for byte in data:
        result ^= byte
    return result


def verify_xor(payload: bytes, expected_xor: int) -> bool:
    """
    验证XOR校验值

    Args:
        payload: 需要校验的数据（版本号+长度+命令码+数据段）
        expected_xor: 期望的校验值

    Returns:
        bool: 校验是否通过
    """
    actual_xor = calculate_xor(payload)
    return actual_xor == expected_xor


def build_checksum(version: int, length: int, cmd: int, data: bytes) -> int:
    """
    构建校验值

    Args:
        version: 协议版本号
        length: 数据段长度（大端序2字节）
        cmd: 命令码
        data: 数据段

    Returns:
        int: XOR校验值
    """
    #构建需要校验的部分：版本号+长度(2字节)+命令码+数据段
    payload = bytes([version]) + length.to_bytes(2, 'big') + bytes([cmd]) + data
    return calculate_xor(payload)
