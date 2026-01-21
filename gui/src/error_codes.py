#-*- coding: utf-8 -*-
"""
错误码映射模块

定义客户端返回的错误码及其中文描述
"""

#错误码定义
ERROR_CODES = {
    #成功
    0x0000: "操作成功",

    #相机错误 (0x01xx)
    0x0101: "相机未连接",
    0x0102: "相机初始化失败",
    0x0103: "采集超时",
    0x0104: "参数设置失败",
    0x0105: "相机断线",
    0x0106: "不支持的分辨率",

    #存储错误 (0x02xx)
    0x0201: "磁盘空间不足",
    0x0202: "写入权限不足",
    0x0203: "文件创建失败",

    #状态冲突 (0x03xx)
    0x0301: "正在录像",
    0x0302: "正在拍照",
    0x0303: "预览未开启",
    0x0304: "预览已开启",

    #协议错误 (0x04xx)
    0x0401: "XOR校验失败",
    0x0402: "帧格式错误",
    0x0403: "未知命令码",
    0x0404: "数据段长度错误",
    0x0405: "协议版本不兼容",

    #编码错误 (0x05xx)
    0x0501: "JPEG编码失败",
    0x0502: "H.264编码失败",
    0x0503: "视频编码器初始化失败",

    #未知错误
    0xFFFF: "未知错误",
}


def get_error_message(code: int) -> str:
    """
    获取错误码对应的中文描述

    Args:
        code: 错误码（2字节整数）

    Returns:
        错误描述字符串
    """
    return ERROR_CODES.get(code, f"未知错误码: 0x{code:04X}")


def get_error_category(code: int) -> str:
    """
    获取错误码所属类别

    Args:
        code: 错误码

    Returns:
        类别名称
    """
    category = (code >> 8) & 0xFF
    categories = {
        0x00: "成功",
        0x01: "相机错误",
        0x02: "存储错误",
        0x03: "状态冲突",
        0x04: "协议错误",
        0x05: "编码错误",
        0xFF: "未知错误",
    }
    return categories.get(category, "未知类别")


def is_success(code: int) -> bool:
    """
    判断是否为成功状态

    Args:
        code: 错误码

    Returns:
        True表示成功，False表示失败
    """
    return code == 0x0000


if __name__ == '__main__':
    #测试代码
    test_codes = [0x0000, 0x0101, 0x0201, 0x0301, 0x0401, 0x0501, 0xFFFF, 0x9999]
    for code in test_codes:
        msg = get_error_message(code)
        cat = get_error_category(code)
        print(f"0x{code:04X}: [{cat}] {msg}")
