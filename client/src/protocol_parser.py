"""
协议解析模块

负责协议帧的解析、构建和验证
帧结构：FE FE [版本号] [长度2字节] [命令码] [数据段] [XOR校验] EF EF
"""
import struct
from enum import IntEnum
from dataclasses import dataclass
from typing import Optional, Tuple, List

from utils.xor_checksum import calculate_xor, verify_xor
from utils.errors import ErrorCode


#协议常量
FRAME_HEADER = b'\xFE\xFE'      #帧头
FRAME_FOOTER = b'\xEF\xEF'      #帧尾
PROTOCOL_VERSION = 0x20          #协议版本2.0
PROTOCOL_MAJOR_VERSION = 2       #主版本号

#安全限制常量
MAX_BUFFER_SIZE = 1 * 1024 * 1024    #缓冲区最大1MB，防止内存耗尽
MAX_DATA_LENGTH = 10 * 1024 * 1024   #数据段最大10MB，防止恶意大包

#帧结构偏移量
HEADER_SIZE = 2                  #帧头长度
VERSION_OFFSET = 2               #版本号偏移
LENGTH_OFFSET = 3                #长度字段偏移
LENGTH_SIZE = 4                  #长度字段长度（4字节，支持大数据帧）
CMD_OFFSET = 7                   #命令码偏移
DATA_OFFSET = 8                  #数据段偏移
FOOTER_SIZE = 2                  #帧尾长度
XOR_SIZE = 1                     #校验字段长度

#最小帧长度：帧头(2)+版本(1)+长度(4)+命令(1)+校验(1)+帧尾(2)=11
MIN_FRAME_SIZE = 11


class CommandCode(IntEnum):
    """命令码枚举"""

    #========== 控制命令（上位机→客户端）==========
    CAPTURE_SINGLE = 0x10          #单次拍照
    RECORD_START = 0x11            #开始录像
    RECORD_STOP = 0x12             #停止录像
    PREVIEW_START = 0x13           #开启实时预览
    PREVIEW_STOP = 0x14            #停止实时预览
    CONTINUOUS_START = 0x15        #开始连续拍照
    CONTINUOUS_STOP = 0x16         #停止连续拍照
    SET_EXPOSURE = 0x20            #设置曝光
    SET_WHITE_BALANCE = 0x21       #设置白平衡
    SET_GAIN = 0x22                #设置增益
    SET_RESOLUTION = 0x23          #设置分辨率
    SET_GAIN_AUTO = 0x24           #设置自动增益
    SET_FRAME_RATE = 0x25          #设置帧率
    SET_PIXEL_FORMAT = 0x26        #设置像素格式
    QUERY_STATUS = 0x30            #查询状态
    QUERY_PARAMS = 0x31            #查询参数
    QUERY_RESOLUTIONS = 0x32       #查询支持的分辨率列表
    QUERY_GAIN_AUTO = 0x33         #查询自动增益状态
    HEARTBEAT = 0xFF               #心跳

    #========== 应答/状态（客户端→上位机）==========
    RESPONSE_SUCCESS = 0x90        #操作成功
    RESPONSE_FAILED = 0x91         #操作失败
    STATUS_REPORT = 0xA0           #状态上报
    PARAMS_REPORT = 0xA1           #参数上报
    RESOLUTIONS_REPORT = 0xA2      #分辨率列表上报
    GAIN_AUTO_REPORT = 0xA3        #自动增益状态上报
    CAPTURE_COMPLETE = 0xB0        #拍照完成
    RECORD_COMPLETE = 0xB1         #录像完成
    PREVIEW_FRAME = 0xC0           #预览帧数据


@dataclass
class ProtocolFrame:
    """协议帧数据结构"""
    version: int                   #协议版本
    command: int                   #命令码
    data: bytes                    #数据段
    raw_frame: bytes = b''         #原始帧数据


class ProtocolParser:
    """协议解析器"""

    def __init__(self):
        self._buffer = bytearray()  #接收缓冲区

    def feed(self, data: bytes) -> List[ProtocolFrame]:
        """
        向解析器输入数据

        Args:
            data: 接收到的原始字节数据

        Returns:
            List[ProtocolFrame]: 解析出的完整帧列表
        """
        self._buffer.extend(data)

        #安全检查：缓冲区超限时清空，防止内存耗尽攻击
        if len(self._buffer) > MAX_BUFFER_SIZE:
            self._buffer.clear()
            return []

        frames = []

        while True:
            frame = self._try_parse_frame()
            if frame is None:
                break
            frames.append(frame)

        return frames

    def _try_parse_frame(self) -> Optional[ProtocolFrame]:
        """
        尝试从缓冲区解析一个完整帧

        Returns:
            Optional[ProtocolFrame]: 解析成功返回帧对象，否则返回None
        """
        #查找帧头
        header_index = self._buffer.find(FRAME_HEADER)
        if header_index == -1:
            #没有找到帧头，清空无效数据
            self._buffer.clear()
            return None

        #丢弃帧头之前的无效数据
        if header_index > 0:
            del self._buffer[:header_index]

        #检查是否有足够数据解析长度字段（4字节）
        if len(self._buffer) < LENGTH_OFFSET + LENGTH_SIZE:
            return None

        #解析长度字段（大端序，4字节）
        data_length = struct.unpack('>I', self._buffer[LENGTH_OFFSET:LENGTH_OFFSET + LENGTH_SIZE])[0]

        #安全检查：数据长度超限时丢弃帧头，防止恶意大包DoS攻击
        if data_length > MAX_DATA_LENGTH:
            del self._buffer[:HEADER_SIZE]
            return None

        #计算完整帧长度
        #帧头(2)+版本(1)+长度(4)+[命令(1)+数据(N-1)]+XOR(1)+帧尾(2)
        #注意：data_length 已包含命令码(1字节)
        frame_length = HEADER_SIZE + 1 + LENGTH_SIZE + data_length + XOR_SIZE + FOOTER_SIZE

        #检查是否有完整帧
        if len(self._buffer) < frame_length:
            return None

        #提取帧数据
        raw_frame = bytes(self._buffer[:frame_length])

        #验证帧尾
        if raw_frame[-FOOTER_SIZE:] != FRAME_FOOTER:
            #帧尾不匹配，丢弃帧头，继续查找
            del self._buffer[:HEADER_SIZE]
            return None

        #提取各字段
        version = raw_frame[VERSION_OFFSET]
        cmd = raw_frame[CMD_OFFSET]
        # data_length 包含命令码(1字节)，所以数据段长度是 data_length - 1
        data = raw_frame[DATA_OFFSET:DATA_OFFSET + data_length - 1]
        xor_byte = raw_frame[-(XOR_SIZE + FOOTER_SIZE)]

        #验证XOR校验
        #校验范围：版本号+长度+命令码+数据段
        payload = raw_frame[VERSION_OFFSET:-(XOR_SIZE + FOOTER_SIZE)]
        if not verify_xor(payload, xor_byte):
            #校验失败，丢弃帧头，继续查找
            del self._buffer[:HEADER_SIZE]
            return None

        #移除已解析的帧
        del self._buffer[:frame_length]

        return ProtocolFrame(
            version=version,
            command=cmd,
            data=data,
            raw_frame=raw_frame
        )

    def clear(self):
        """清空缓冲区"""
        self._buffer.clear()

    @property
    def buffer_size(self) -> int:
        """获取当前缓冲区大小"""
        return len(self._buffer)


class ProtocolBuilder:
    """协议帧构建器"""

    @staticmethod
    def build_frame(cmd: int, data: bytes = b'', version: int = PROTOCOL_VERSION) -> bytes:
        """
        构建协议帧

        Args:
            cmd: 命令码
            data: 数据段
            version: 协议版本号

        Returns:
            bytes: 完整的协议帧
        """
        # 长度 = 命令码(1字节) + 数据段长度
        length = 1 + len(data)

        #构建需要校验的部分：版本号+长度(4字节)+命令码+数据段
        payload = bytes([version]) + struct.pack('>I', length) + bytes([cmd]) + data

        #计算XOR校验
        xor_byte = calculate_xor(payload)

        #组装完整帧
        frame = FRAME_HEADER + payload + bytes([xor_byte]) + FRAME_FOOTER
        return frame

    @staticmethod
    def build_success_response(original_cmd: int) -> bytes:
        """
        构建成功响应帧

        Args:
            original_cmd: 原始命令码

        Returns:
            bytes: 成功响应帧
        """
        return ProtocolBuilder.build_frame(
            CommandCode.RESPONSE_SUCCESS,
            bytes([original_cmd])
        )

    @staticmethod
    def build_error_response(original_cmd: int, error_code: int) -> bytes:
        """
        构建错误响应帧

        Args:
            original_cmd: 原始命令码
            error_code: 错误码

        Returns:
            bytes: 错误响应帧
        """
        data = bytes([original_cmd]) + struct.pack('>H', error_code)
        return ProtocolBuilder.build_frame(CommandCode.RESPONSE_FAILED, data)

    @staticmethod
    def build_heartbeat_response() -> bytes:
        """
        构建心跳响应帧

        Returns:
            bytes: 心跳响应帧
        """
        return ProtocolBuilder.build_success_response(CommandCode.HEARTBEAT)

    @staticmethod
    def build_status_report(status_data: bytes) -> bytes:
        """
        构建状态上报帧

        Args:
            status_data: 状态数据

        Returns:
            bytes: 状态上报帧
        """
        return ProtocolBuilder.build_frame(CommandCode.STATUS_REPORT, status_data)

    @staticmethod
    def build_params_report(params_data: bytes) -> bytes:
        """
        构建参数上报帧

        Args:
            params_data: 参数数据

        Returns:
            bytes: 参数上报帧
        """
        return ProtocolBuilder.build_frame(CommandCode.PARAMS_REPORT, params_data)

    @staticmethod
    def build_resolutions_report(resolutions: List[Tuple[int, int]]) -> bytes:
        """
        构建分辨率列表上报帧

        Args:
            resolutions: 分辨率列表[(宽, 高), ...]

        Returns:
            bytes: 分辨率列表上报帧
        """
        data = bytes([len(resolutions)])
        for width, height in resolutions:
            data += struct.pack('>HH', width, height)
        return ProtocolBuilder.build_frame(CommandCode.RESOLUTIONS_REPORT, data)

    @staticmethod
    def build_gain_auto_report(enabled: bool) -> bytes:
        """
        构建自动增益状态上报帧

        Args:
            enabled: 自动增益是否启用

        Returns:
            bytes: 自动增益状态上报帧
        """
        data = bytes([1 if enabled else 0])
        return ProtocolBuilder.build_frame(CommandCode.GAIN_AUTO_REPORT, data)

    @staticmethod
    def build_capture_complete(filename: str) -> bytes:
        """
        构建拍照完成通知帧

        Args:
            filename: 文件名

        Returns:
            bytes: 拍照完成通知帧
        """
        filename_bytes = filename.encode('utf-8')
        data = bytes([len(filename_bytes)]) + filename_bytes
        return ProtocolBuilder.build_frame(CommandCode.CAPTURE_COMPLETE, data)

    @staticmethod
    def build_record_complete(filename: str) -> bytes:
        """
        构建录像完成通知帧

        Args:
            filename: 文件名

        Returns:
            bytes: 录像完成通知帧
        """
        filename_bytes = filename.encode('utf-8')
        data = bytes([len(filename_bytes)]) + filename_bytes
        return ProtocolBuilder.build_frame(CommandCode.RECORD_COMPLETE, data)

    @staticmethod
    def build_preview_frame(seq: int, jpeg_data: bytes) -> bytes:
        """
        构建预览帧数据包

        Args:
            seq: 帧序号
            jpeg_data: JPEG图像数据

        Returns:
            bytes: 预览帧数据包
        """
        data = struct.pack('>I', seq) + struct.pack('>I', len(jpeg_data)) + jpeg_data
        return ProtocolBuilder.build_frame(CommandCode.PREVIEW_FRAME, data)


def check_version_compatible(version: int) -> Tuple[bool, Optional[int]]:
    """
    检查协议版本兼容性

    Args:
        version: 接收到的版本号

    Returns:
        Tuple[bool, Optional[int]]: (是否兼容, 错误码或None)
    """
    #提取主版本号（高4位）
    major_version = (version >> 4) & 0x0F

    if major_version != PROTOCOL_MAJOR_VERSION:
        return False, ErrorCode.PROTOCOL_VERSION_MISMATCH

    return True, None
