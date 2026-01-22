# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 配置文件 - Basler Camera GUI

使用方法:
    pyinstaller --clean --noconfirm build.spec
"""

import os
import sys
from pathlib import Path

# 获取项目路径
spec_dir = os.path.dirname(os.path.abspath(SPEC))
src_dir = os.path.join(spec_dir, 'src')

block_cipher = None

# 分析阶段
a = Analysis(
    # 入口脚本
    [os.path.join(src_dir, 'main.py')],

    # 额外的搜索路径
    pathex=[src_dir],

    # 二进制文件（DLL等）
    binaries=[],

    # 数据文件
    datas=[
        # 配置文件
        (os.path.join(spec_dir, 'config'), 'config'),
    ],

    # 隐式导入（PyInstaller可能检测不到的模块）
    hiddenimports=[
        # GUI框架
        'tkinter',
        'tkinter.ttk',
        'tkinter.messagebox',
        'tkinter.filedialog',
        'tkinter.simpledialog',

        # 图像处理
        'numpy',
        'PIL',
        'PIL.Image',
        'PIL.ImageTk',

        # 日志
        'loguru',
        'loguru._logger',

        # 异步
        'asyncio',
        'asyncio.events',
        'asyncio.base_events',

        # 标准库
        'json',
        'struct',
        'threading',
        'queue',
        'dataclasses',
        'enum',
        'typing',
        'pathlib',
        'datetime',
        'collections',
        'socket',
        'io',
    ],

    # Hook路径
    hookspath=[],

    # Hook配置
    hooksconfig={},

    # 运行时Hook
    runtime_hooks=[],

    # 排除的模块
    excludes=[
        # 测试模块
        'pytest',
        'unittest',

        # 开发工具
        'IPython',
        'jupyter',

        # 不需要的GUI框架
        'PyQt5',
        'PyQt6',
        'PySide2',
        'PySide6',
        'wx',

        # 相机SDK（GUI不需要）
        'pypylon',
        'cv2',

        # 其他
        'matplotlib',
        'scipy',
        'pandas',
    ],

    # Windows特定选项
    win_no_prefer_redirects=False,
    win_private_assemblies=False,

    # 加密
    cipher=block_cipher,

    # 不使用归档
    noarchive=False,
)

# PYZ归档
pyz = PYZ(
    a.pure,
    a.zipped_data,
    cipher=block_cipher,
)

# 可执行文件
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],

    # 输出名称
    name='BaslerCameraGUI',

    # 调试模式
    debug=False,

    # 引导程序选项
    bootloader_ignore_signals=False,

    # 去除符号表
    strip=False,

    # UPX压缩
    upx=True,
    upx_exclude=[],

    # 运行时临时目录
    runtime_tmpdir=None,

    # 无控制台窗口（GUI应用）
    console=False,

    # 禁用窗口化回溯
    disable_windowed_traceback=False,

    # argv模拟
    argv_emulation=False,

    # 目标架构
    target_arch=None,

    # 代码签名
    codesign_identity=None,
    entitlements_file=None,

    # 图标（如果有的话）
    # icon='icon.ico',
)
