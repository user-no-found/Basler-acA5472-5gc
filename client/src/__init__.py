"""
Basler相机控制系统 - 客户端模块
"""
from .tcp_server import TCPServer
from .protocol_parser import (
    ProtocolParser,
    ProtocolBuilder,
    ProtocolFrame,
    CommandCode,
    check_version_compatible,
    PROTOCOL_VERSION,
)
from .utils.errors import ErrorCode, get_error_description, get_error_category
from .utils.xor_checksum import calculate_xor, verify_xor, build_checksum

__version__ = '1.0.0'

__all__ = [
    'TCPServer',
    'ProtocolParser',
    'ProtocolBuilder',
    'ProtocolFrame',
    'CommandCode',
    'check_version_compatible',
    'PROTOCOL_VERSION',
    'ErrorCode',
    'get_error_description',
    'get_error_category',
    'calculate_xor',
    'verify_xor',
    'build_checksum',
]
