"""
错误码定义模块

定义系统所有错误码及其描述
错误码格式：0xXXYY
- XX: 错误类别
- YY: 具体错误
"""
from enum import IntEnum


class ErrorCode(IntEnum):
    """错误码枚举"""

    #无错误
    SUCCESS = 0x0000

    #========== 相机错误 (0x01xx) ==========
    CAMERA_NOT_CONNECTED = 0x0101      #相机未连接，pylon未检测到相机
    CAMERA_INIT_FAILED = 0x0102        #相机初始化失败，pylon初始化异常
    CAMERA_GRAB_TIMEOUT = 0x0103       #采集超时，图像抓取超时
    CAMERA_PARAM_FAILED = 0x0104       #参数设置失败，参数超出范围
    CAMERA_DISCONNECTED = 0x0105       #相机断线，采集过程中相机断开
    CAMERA_UNSUPPORTED_RES = 0x0106    #不支持的分辨率，设置的分辨率相机不支持

    #========== 文件系统错误 (0x02xx) ==========
    DISK_SPACE_LOW = 0x0201            #磁盘空间不足，无法保存图像/视频
    WRITE_PERMISSION_DENIED = 0x0202   #写入权限不足，目标目录无写权限
    FILE_CREATE_FAILED = 0x0203        #文件创建失败，文件路径非法或已存在

    #========== 状态冲突错误 (0x03xx) ==========
    STATE_RECORDING = 0x0301           #正在录像，录像时禁止拍照
    STATE_CAPTURING = 0x0302           #正在拍照，拍照时禁止录像
    PREVIEW_NOT_STARTED = 0x0303       #预览未开启，尝试停止未开启的预览
    PREVIEW_ALREADY_STARTED = 0x0304   #预览已开启，重复开启预览

    #========== 协议错误 (0x04xx) ==========
    XOR_CHECK_FAILED = 0x0401          #XOR校验失败，数据传输损坏
    FRAME_FORMAT_ERROR = 0x0402        #帧格式错误，帧头/帧尾不匹配
    UNKNOWN_COMMAND = 0x0403           #未知命令码，无法识别的命令
    DATA_LENGTH_ERROR = 0x0404         #数据段长度错误，数据段与命令不匹配
    PROTOCOL_VERSION_MISMATCH = 0x0405 #协议版本不兼容，主版本号不匹配

    #========== 编码错误 (0x05xx) ==========
    JPEG_ENCODE_FAILED = 0x0501        #JPEG编码失败，图像编码异常
    H264_ENCODE_FAILED = 0x0502        #H.264编码失败，视频编码异常
    VIDEO_WRITER_INIT_FAILED = 0x0503  #视频编码器初始化失败，无法创建VideoWriter

    #========== 未知错误 ==========
    UNKNOWN_ERROR = 0xFFFF             #未知错误，未分类的异常


#错误码描述映射
ERROR_DESCRIPTIONS = {
    ErrorCode.SUCCESS: "操作成功",

    #相机错误
    ErrorCode.CAMERA_NOT_CONNECTED: "相机未连接",
    ErrorCode.CAMERA_INIT_FAILED: "相机初始化失败",
    ErrorCode.CAMERA_GRAB_TIMEOUT: "图像采集超时",
    ErrorCode.CAMERA_PARAM_FAILED: "参数设置失败",
    ErrorCode.CAMERA_DISCONNECTED: "相机断线",
    ErrorCode.CAMERA_UNSUPPORTED_RES: "不支持的分辨率",

    #文件系统错误
    ErrorCode.DISK_SPACE_LOW: "磁盘空间不足",
    ErrorCode.WRITE_PERMISSION_DENIED: "写入权限不足",
    ErrorCode.FILE_CREATE_FAILED: "文件创建失败",

    #状态冲突错误
    ErrorCode.STATE_RECORDING: "正在录像中",
    ErrorCode.STATE_CAPTURING: "正在拍照中",
    ErrorCode.PREVIEW_NOT_STARTED: "预览未开启",
    ErrorCode.PREVIEW_ALREADY_STARTED: "预览已开启",

    #协议错误
    ErrorCode.XOR_CHECK_FAILED: "XOR校验失败",
    ErrorCode.FRAME_FORMAT_ERROR: "帧格式错误",
    ErrorCode.UNKNOWN_COMMAND: "未知命令码",
    ErrorCode.DATA_LENGTH_ERROR: "数据段长度错误",
    ErrorCode.PROTOCOL_VERSION_MISMATCH: "协议版本不兼容",

    #编码错误
    ErrorCode.JPEG_ENCODE_FAILED: "JPEG编码失败",
    ErrorCode.H264_ENCODE_FAILED: "H.264编码失败",
    ErrorCode.VIDEO_WRITER_INIT_FAILED: "视频编码器初始化失败",

    #未知错误
    ErrorCode.UNKNOWN_ERROR: "未知错误",
}


def get_error_description(code: int) -> str:
    """
    获取错误码描述

    Args:
        code: 错误码

    Returns:
        str: 错误描述
    """
    try:
        error_code = ErrorCode(code)
        return ERROR_DESCRIPTIONS.get(error_code, f"未知错误码: 0x{code:04X}")
    except ValueError:
        return f"未知错误码: 0x{code:04X}"


def get_error_category(code: int) -> str:
    """
    获取错误类别

    Args:
        code: 错误码

    Returns:
        str: 错误类别名称
    """
    category = (code >> 8) & 0xFF
    categories = {
        0x00: "无错误",
        0x01: "相机错误",
        0x02: "文件系统错误",
        0x03: "状态冲突错误",
        0x04: "协议错误",
        0x05: "编码错误",
        0xFF: "未知错误",
    }
    return categories.get(category, "未知类别")
