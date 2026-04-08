"""
TCP服务器模块

基于asyncio实现的异步TCP服务器
负责：
- 监听端口接受连接
- 协议帧解析和分发
- 多客户端管理（同时只有一个可控制）
- 心跳检测
- 状态广播
- 状态/参数/分辨率查询
- 拍照/录像控制

性能优化:
- TCP_NODELAY禁用Nagle算法
- 调整发送/接收缓冲区大小
- 批量发送优化
"""
import asyncio
import struct
import os
import socket
import threading
import time
from typing import Dict, Optional, Callable, Awaitable, Any, TYPE_CHECKING
from dataclasses import dataclass, field
from enum import IntEnum
from datetime import datetime
from collections import deque

from loguru import logger

try:
    import cv2
    OPENCV_AVAILABLE = True
except ImportError:
    cv2 = None
    OPENCV_AVAILABLE = False
    logger.warning("OpenCV未安装，录像实时预览回传功能不可用")

from protocol_parser import (
    ProtocolParser,
    ProtocolBuilder,
    ProtocolFrame,
    CommandCode,
    check_version_compatible,
    PROTOCOL_VERSION,
)
from utils.errors import ErrorCode, get_error_description

if TYPE_CHECKING:
    from camera_controller import CameraController
    from image_processor import ImageProcessor
    from image_acquisition import ImageAcquisition, PreviewAcquisition


class ClientState(IntEnum):
    """客户端状态"""
    CONNECTED = 0       #已连接
    AUTHENTICATED = 1   #已认证（版本检查通过）
    CONTROLLING = 2     #控制中（当前控制者）


@dataclass
class ClientInfo:
    """客户端信息"""
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    address: tuple
    state: ClientState = ClientState.CONNECTED
    parser: ProtocolParser = field(default_factory=ProtocolParser)
    last_heartbeat: datetime = field(default_factory=datetime.now)
    connected_at: datetime = field(default_factory=datetime.now)


#命令处理器类型
CommandHandler = Callable[[ClientInfo, ProtocolFrame], Awaitable[Optional[bytes]]]


class TCPServer:
    """异步TCP服务器"""

    #分辨率索引映射
    RESOLUTION_MAP = {
        0: (5472, 3648),
        1: (4096, 2160),
        2: (3840, 2160),
        3: (2736, 1824),
        4: (1920, 1080),
        5: (1280, 720),
        6: (640, 480),
    }
    #像素格式索引映射
    PIXEL_FORMAT_MAP = {
        0: "BayerRG8",
        1: "BayerRG12",
        2: "BGR8",
        3: "RGB8",
        4: "Mono8",
    }
    #协议中录像/预览帧率字段为1字节
    PROTOCOL_FPS_MIN = 1
    PROTOCOL_FPS_MAX = 255
    RECORD_PREVIEW_JPEG_QUALITY = 80
    RECORD_PREVIEW_MAX_EDGE = 960
    # 外部闪光灯触发（TCP）
    FLASH_TRIGGER_HOST = "192.168.1.201"
    FLASH_TRIGGER_PORT = 2000
    FLASH_TRIGGER_TIMEOUT_SEC = 0.5
    FLASH_TRIGGER_PAYLOAD = b"\xAA\xAA"
    # 连拍周期（秒）
    CONTINUOUS_INTERVAL_SEC = 1.0

    #========== 性能优化常量 ==========
    #发送缓冲区大小（64KB）
    SEND_BUFFER_SIZE = 65536
    #接收缓冲区大小（64KB）
    RECV_BUFFER_SIZE = 65536
    #读取块大小（8KB）
    READ_CHUNK_SIZE = 8192
    #发送队列最大长度
    SEND_QUEUE_MAX_SIZE = 100

    def __init__(self, host: str = '0.0.0.0', port: int = 8899):
        """
        初始化TCP服务器

        Args:
            host: 监听地址
            port: 监听端口
        """
        self._host = host
        self._port = port
        self._server: Optional[asyncio.Server] = None
        self._clients: Dict[str, ClientInfo] = {}  #客户端字典，key为地址字符串
        self._controller_id: Optional[str] = None  #当前控制者ID
        self._running = False

        #命令处理器映射
        self._handlers: Dict[int, CommandHandler] = {}

        #注册内置命令处理器
        self._register_builtin_handlers()

        #心跳超时时间（秒）
        self._heartbeat_timeout = 30

        #状态广播间隔（秒）
        self._status_broadcast_interval = 1.0

        #状态广播任务
        self._broadcast_task: Optional[asyncio.Task] = None

        #相机控制器引用
        self._camera: Optional['CameraController'] = None

        #图像处理器引用
        self._image_processor: Optional['ImageProcessor'] = None

        #图像采集器引用
        self._image_acquisition: Optional['ImageAcquisition'] = None

        #预览采集器引用
        self._preview_acquisition: Optional['PreviewAcquisition'] = None

        #系统状态标志
        self._is_capturing = False    #正在拍照
        self._is_recording = False    #正在录像
        self._is_previewing = False   #正在预览
        self._is_continuous = False   #正在连续拍照

        #闪光灯状态缓存
        # 保存用户最新配置；录像/预览期间仅缓存，退出后恢复
        self._flash_user_config = {
            "enable": False,
            "delay_ms": 0,
        }
        self._flash_forced_disabled = False
        self._flash_trigger_lock = threading.Lock()

        #录像相关
        self._recording_task: Optional[asyncio.Task] = None
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None

        #连续拍照相关
        self._continuous_thread: Optional[threading.Thread] = None
        self._continuous_stop_event = threading.Event()

        #========== 性能优化：发送队列 ==========
        #每个客户端的发送队列（用于批量发送）
        self._send_queues: Dict[str, deque] = {}
        #发送队列锁
        self._send_lock = asyncio.Lock()

    def _register_builtin_handlers(self):
        """注册内置命令处理器"""
        self.register_handler(CommandCode.HEARTBEAT, self._handle_heartbeat)
        #注册拍照处理器
        self.register_handler(CommandCode.CAPTURE_SINGLE, self._handle_capture)
        #注册状态查询处理器
        self.register_handler(CommandCode.QUERY_STATUS, self._handle_query_status)
        self.register_handler(CommandCode.QUERY_PARAMS, self._handle_query_params)
        self.register_handler(CommandCode.QUERY_RESOLUTIONS, self._handle_query_resolutions)
        #注册参数设置处理器
        self.register_handler(CommandCode.SET_EXPOSURE, self._handle_set_exposure)
        self.register_handler(CommandCode.SET_WHITE_BALANCE, self._handle_set_white_balance)
        self.register_handler(CommandCode.SET_GAIN, self._handle_set_gain)
        self.register_handler(CommandCode.SET_RESOLUTION, self._handle_set_resolution)
        self.register_handler(CommandCode.SET_GAIN_AUTO, self._handle_set_gain_auto)
        self.register_handler(CommandCode.SET_FRAME_RATE, self._handle_set_frame_rate)
        self.register_handler(CommandCode.SET_PIXEL_FORMAT, self._handle_set_pixel_format)
        self.register_handler(CommandCode.SET_FLASH, self._handle_set_flash)
        #注册录像控制处理器
        self.register_handler(CommandCode.RECORD_START, self._handle_record_start)
        self.register_handler(CommandCode.RECORD_STOP, self._handle_record_stop)
        #注册预览控制处理器
        self.register_handler(CommandCode.PREVIEW_START, self._handle_preview_start)
        self.register_handler(CommandCode.PREVIEW_STOP, self._handle_preview_stop)
        #注册连续拍照处理器
        self.register_handler(CommandCode.CONTINUOUS_START, self._handle_continuous_start)
        self.register_handler(CommandCode.CONTINUOUS_STOP, self._handle_continuous_stop)

    def set_camera(self, camera: 'CameraController') -> None:
        """
        设置相机控制器引用

        Args:
            camera: 相机控制器实例
        """
        self._camera = camera
        logger.info("TCP服务器已绑定相机控制器")

    def set_image_processor(self, processor: 'ImageProcessor') -> None:
        """
        设置图像处理器引用

        Args:
            processor: 图像处理器实例
        """
        self._image_processor = processor
        logger.info("TCP服务器已绑定图像处理器")

    def set_image_acquisition(self, acquisition: 'ImageAcquisition') -> None:
        """
        设置图像采集器引用

        Args:
            acquisition: 图像采集器实例
        """
        self._image_acquisition = acquisition
        logger.info("TCP服务器已绑定图像采集器")

    def set_preview_acquisition(self, preview: 'PreviewAcquisition') -> None:
        """
        设置预览采集器引用

        Args:
            preview: 预览采集器实例
        """
        self._preview_acquisition = preview
        #设置预览帧回调
        preview.set_preview_callback(self._on_preview_frame)
        logger.info("TCP服务器已绑定预览采集器")

    def _normalize_fps(self, requested_fps: int) -> int:
        """
        按协议范围与相机能力范围动态裁剪帧率。

        Args:
            requested_fps: 请求帧率

        Returns:
            int: 裁剪后的帧率
        """
        fps = max(self.PROTOCOL_FPS_MIN, min(self.PROTOCOL_FPS_MAX, int(requested_fps)))

        if self._camera and hasattr(self._camera, "get_frame_rate_range"):
            try:
                min_fps, max_fps = self._camera.get_frame_rate_range()
                if max_fps > 0 and max_fps >= min_fps:
                    if min_fps > 0 and fps < min_fps:
                        fps = int(min_fps)
                    if fps > max_fps:
                        fps = int(max_fps)
            except Exception as e:
                logger.warning(f"读取相机帧率范围失败，沿用协议范围裁剪: {e}")

        if fps < self.PROTOCOL_FPS_MIN:
            fps = self.PROTOCOL_FPS_MIN
        return fps

    @staticmethod
    def _sanitize_error_detail(error: Exception, max_len: int = 200) -> str:
        """
        清洗异常文本，避免过长或包含换行。
        """
        detail = str(error).strip().replace("\r", " ").replace("\n", " ")
        detail = " ".join(detail.split())
        if not detail:
            detail = error.__class__.__name__
        if len(detail) > max_len:
            detail = detail[:max_len]
        return detail

    @staticmethod
    def _infer_error_code_from_detail(detail: str, default_code: int = ErrorCode.UNKNOWN_ERROR) -> int:
        """
        根据异常摘要推断更具体的错误码。
        """
        text = detail.lower()
        if "node not existing" in text or "acquisitionframerate" in text or "setvalue" in text:
            return ErrorCode.CAMERA_PARAM_FAILED
        if "openh264" in text or "videowriter" in text or "ffmpeg" in text:
            return ErrorCode.VIDEO_WRITER_INIT_FAILED
        if "disk" in text or "space" in text or "no space" in text:
            return ErrorCode.DISK_SPACE_LOW
        if "permission" in text or "access denied" in text:
            return ErrorCode.WRITE_PERMISSION_DENIED
        if "timeout" in text:
            return ErrorCode.CAMERA_GRAB_TIMEOUT
        return default_code

    def _build_exception_error_response(self, frame_cmd: int, error: Exception,
                                        default_code: int = ErrorCode.UNKNOWN_ERROR) -> bytes:
        """
        构建带异常详情的错误响应，优先返回可推断的具体错误码。
        """
        detail = self._sanitize_error_detail(error)
        code = self._infer_error_code_from_detail(detail, default_code=default_code)
        return ProtocolBuilder.build_error_response(frame_cmd, code, detail)

    def register_handler(self, cmd: int, handler: CommandHandler):
        """
        注册命令处理器

        Args:
            cmd: 命令码
            handler: 处理器函数
        """
        self._handlers[cmd] = handler
        logger.debug(f"注册命令处理器: 0x{cmd:02X}")

    async def start(self):
        """启动服务器"""
        self._server = await asyncio.start_server(
            self._handle_client,
            self._host,
            self._port
        )

        self._running = True
        self._event_loop = asyncio.get_event_loop()
        addr = self._server.sockets[0].getsockname()
        logger.info(f"TCP服务器启动: {addr[0]}:{addr[1]}")

        #启动状态广播任务
        self._broadcast_task = asyncio.create_task(self._status_broadcast_loop())

        async with self._server:
            await self._server.serve_forever()

    async def stop(self):
        """停止服务器"""
        self._running = False

        #取消广播任务
        if self._broadcast_task:
            self._broadcast_task.cancel()
            try:
                await self._broadcast_task
            except asyncio.CancelledError:
                pass

        #关闭所有客户端连接
        for client_id, client in list(self._clients.items()):
            await self._close_client(client_id, "服务器关闭")

        #关闭服务器
        if self._server:
            self._server.close()
            await self._server.wait_closed()

        logger.info("TCP服务器已停止")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """
        处理客户端连接

        Args:
            reader: 读取流
            writer: 写入流
        """
        addr = writer.get_extra_info('peername')
        client_id = f"{addr[0]}:{addr[1]}"

        #========== 性能优化：设置socket选项 ==========
        sock = writer.get_extra_info('socket')
        if sock:
            try:
                #禁用Nagle算法，减少小数据包延迟
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                #设置发送缓冲区大小
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, self.SEND_BUFFER_SIZE)
                #设置接收缓冲区大小
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self.RECV_BUFFER_SIZE)
                logger.debug(f"客户端 {client_id} socket优化已应用: TCP_NODELAY=1, SNDBUF={self.SEND_BUFFER_SIZE}, RCVBUF={self.RECV_BUFFER_SIZE}")
            except Exception as e:
                logger.warning(f"设置socket选项失败: {e}")

        #创建客户端信息
        client = ClientInfo(
            reader=reader,
            writer=writer,
            address=addr
        )
        self._clients[client_id] = client

        #初始化发送队列
        self._send_queues[client_id] = deque(maxlen=self.SEND_QUEUE_MAX_SIZE)

        logger.info(f"客户端连接: {client_id}")

        #如果没有控制者，设置为控制者
        if self._controller_id is None:
            self._controller_id = client_id
            client.state = ClientState.CONTROLLING
            logger.info(f"客户端 {client_id} 成为控制者")

        try:
            await self._client_loop(client_id, client)
        except asyncio.CancelledError:
            logger.info(f"客户端 {client_id} 连接被取消")
        except Exception as e:
            logger.error(f"客户端 {client_id} 处理异常: {e}")
        finally:
            await self._close_client(client_id, "连接断开")

    async def _client_loop(self, client_id: str, client: ClientInfo):
        """
        客户端主循环

        Args:
            client_id: 客户端ID
            client: 客户端信息
        """
        while self._running:
            try:
                #读取数据，设置超时，使用优化的块大小
                data = await asyncio.wait_for(
                    client.reader.read(self.READ_CHUNK_SIZE),
                    timeout=self._heartbeat_timeout
                )

                if not data:
                    #连接关闭
                    logger.info(f"客户端 {client_id} 断开连接")
                    break

                #解析协议帧
                frames = client.parser.feed(data)

                for frame in frames:
                    await self._process_frame(client_id, client, frame)

            except asyncio.TimeoutError:
                #心跳超时
                logger.warning(f"客户端 {client_id} 心跳超时")
                break
            except ConnectionResetError:
                logger.info(f"客户端 {client_id} 连接重置")
                break
            except Exception as e:
                logger.error(f"客户端 {client_id} 读取异常: {e}")
                break

    async def _process_frame(self, client_id: str, client: ClientInfo, frame: ProtocolFrame):
        """
        处理协议帧

        Args:
            client_id: 客户端ID
            client: 客户端信息
            frame: 协议帧
        """
        #检查协议版本
        compatible, error_code = check_version_compatible(frame.version)
        if not compatible:
            logger.warning(f"客户端 {client_id} 协议版本不兼容: 0x{frame.version:02X}")
            response = ProtocolBuilder.build_error_response(frame.command, error_code)
            await self._send_to_client(client, response)
            return

        #更新心跳时间
        client.last_heartbeat = datetime.now()

        logger.debug(f"收到命令 0x{frame.command:02X} 来自 {client_id}, 数据长度: {len(frame.data)}")

        #检查是否有控制权限（非心跳命令需要控制权限）
        if frame.command != CommandCode.HEARTBEAT:
            if client_id != self._controller_id:
                logger.warning(f"客户端 {client_id} 无控制权限")
                response = ProtocolBuilder.build_error_response(
                    frame.command,
                    ErrorCode.UNKNOWN_ERROR  #可以定义专门的权限错误码
                )
                await self._send_to_client(client, response)
                return

        #查找并执行命令处理器
        handler = self._handlers.get(frame.command)
        if handler:
            try:
                response = await handler(client, frame)
                if response:
                    await self._send_to_client(client, response)
            except Exception as e:
                logger.error(f"命令 0x{frame.command:02X} 处理异常: {e}")
                response = ProtocolBuilder.build_error_response(
                    frame.command,
                    ErrorCode.UNKNOWN_ERROR
                )
                await self._send_to_client(client, response)
        else:
            #未知命令
            logger.warning(f"未知命令码: 0x{frame.command:02X}")
            response = ProtocolBuilder.build_error_response(
                frame.command,
                ErrorCode.UNKNOWN_COMMAND
            )
            await self._send_to_client(client, response)

    async def _handle_heartbeat(self, client: ClientInfo, frame: ProtocolFrame) -> Optional[bytes]:
        """
        处理心跳命令

        Args:
            client: 客户端信息
            frame: 协议帧

        Returns:
            Optional[bytes]: 响应数据
        """
        return ProtocolBuilder.build_heartbeat_response()

    #========== 状态查询处理器 ==========

    async def _handle_query_status(self, client: ClientInfo, frame: ProtocolFrame) -> Optional[bytes]:
        """
        处理状态查询命令(0x30)

        状态字节结构:
        - bit 0: 相机连接状态 (1=已连接)
        - bit 1: 正在拍照 (1=是)
        - bit 2: 正在录像 (1=是)
        - bit 3: 正在预览 (1=是)
        - bit 4-7: 保留

        Args:
            client: 客户端信息
            frame: 协议帧

        Returns:
            Optional[bytes]: 响应数据(0xA0状态上报)
        """
        status_byte = self._build_status_byte()
        logger.debug(f"状态查询响应: 0x{status_byte:02X}")
        return ProtocolBuilder.build_status_report(bytes([status_byte]))

    async def _handle_query_params(self, client: ClientInfo, frame: ProtocolFrame) -> Optional[bytes]:
        """
        处理参数查询命令(0x31)

        参数结构体(基础18字节，可扩展到22字节):
        - 曝光模式(1字节): 0-自动, 1-手动
        - 曝光值(4字节): 微秒，大端序
        - 增益(2字节): 大端序（乘以100后取整）
        - 白平衡模式(1字节): 0-自动, 1-手动
        - 白平衡R(2字节): 大端序（乘以100后取整）
        - 白平衡G(2字节): 大端序（固定100，即1.0）
        - 白平衡B(2字节): 大端序（乘以100后取整）
        - 分辨率宽(2字节): 大端序
        - 分辨率高(2字节): 大端序
        - 相机实际帧率(4字节，可选): fps*100，大端序

        Args:
            client: 客户端信息
            frame: 协议帧

        Returns:
            Optional[bytes]: 响应数据(0xA1参数上报)
        """
        params_data = self._build_params_data()
        logger.debug(f"参数查询响应: {len(params_data)} 字节")
        return ProtocolBuilder.build_params_report(params_data)

    async def _handle_query_resolutions(self, client: ClientInfo, frame: ProtocolFrame) -> Optional[bytes]:
        """
        处理分辨率列表查询命令(0x32)

        响应格式:
        - 数量(1字节)
        - [宽1(2字节)高1(2字节)]...[宽N(2字节)高N(2字节)]

        Args:
            client: 客户端信息
            frame: 协议帧

        Returns:
            Optional[bytes]: 响应数据(0xA2分辨率列表上报)
        """
        resolutions = self._get_supported_resolutions()
        logger.debug(f"分辨率列表查询响应: {len(resolutions)} 个分辨率")
        return ProtocolBuilder.build_resolutions_report(resolutions)

    #========== 参数设置处理器 ==========

    async def _handle_set_exposure(self, client: ClientInfo, frame: ProtocolFrame) -> Optional[bytes]:
        """
        处理曝光设置命令(0x20)

        数据格式: [模式(1字节)][值(4字节)]
        - 模式: 0-自动, 1-手动
        - 值: 曝光时间（微秒），大端序

        Args:
            client: 客户端信息
            frame: 协议帧

        Returns:
            Optional[bytes]: 响应数据
        """
        #检查相机是否连接
        if self._camera is None or not self._camera.is_connected:
            logger.warning("设置曝光失败: 相机未连接")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.CAMERA_NOT_CONNECTED
            )

        #检查数据长度
        if len(frame.data) < 5:
            logger.warning(f"设置曝光失败: 数据长度不足，期望5字节，实际{len(frame.data)}字节")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.DATA_LENGTH_ERROR
            )

        try:
            #解析数据
            mode = frame.data[0]  #0-自动, 1-手动
            exposure_us = struct.unpack('>I', frame.data[1:5])[0]  #大端序4字节

            logger.info(f"设置曝光: 模式={mode}, 值={exposure_us}us")

            #导入曝光模式枚举
            from camera_controller import ExposureMode

            if mode == 0:
                #自动曝光
                success = self._camera.set_exposure_auto(True)
                error_code = ErrorCode.CAMERA_PARAM_FAILED if not success else None
            else:
                #手动曝光
                success, error_code = self._camera.set_exposure(exposure_us, ExposureMode.MANUAL)

            if success:
                logger.info(f"曝光设置成功: 模式={'自动' if mode == 0 else '手动'}, 值={exposure_us}us")
                return ProtocolBuilder.build_success_response(frame.command)
            else:
                logger.warning("曝光设置失败: 参数超出范围或相机不支持")
                return ProtocolBuilder.build_error_response(
                    frame.command, error_code or ErrorCode.CAMERA_PARAM_FAILED
                )

        except Exception as e:
            logger.error(f"设置曝光异常: {e}")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.CAMERA_PARAM_FAILED
            )

    async def _handle_set_white_balance(self, client: ClientInfo, frame: ProtocolFrame) -> Optional[bytes]:
        """
        处理白平衡设置命令(0x21)

        数据格式: [模式(1字节)]
        - 模式: 0-连续(Continuous), 1-一次(Once), 2-关闭(Off)

        Args:
            client: 客户端信息
            frame: 协议帧

        Returns:
            Optional[bytes]: 响应数据
        """
        #检查相机是否连接
        if self._camera is None or not self._camera.is_connected:
            logger.warning("设置白平衡失败: 相机未连接")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.CAMERA_NOT_CONNECTED
            )

        #检查数据长度
        if len(frame.data) < 1:
            logger.warning(f"设置白平衡失败: 数据长度不足，期望1字节，实际{len(frame.data)}字节")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.DATA_LENGTH_ERROR
            )

        try:
            #解析数据
            mode = frame.data[0]  #0-连续, 1-一次, 2-关闭

            #导入白平衡模式枚举
            from camera_controller import WhiteBalanceMode

            if mode == 0:
                #连续自动白平衡
                logger.info("设置白平衡: 模式=连续(Continuous)")
                success, error_code = self._camera.set_white_balance(WhiteBalanceMode.CONTINUOUS)
            elif mode == 1:
                #一次自动白平衡
                logger.info("设置白平衡: 模式=一次(Once)")
                success, error_code = self._camera.set_white_balance(WhiteBalanceMode.ONCE)
            else:
                #关闭自动白平衡（使用当前值或默认值）
                logger.info("设置白平衡: 模式=关闭(Off)")
                success, error_code = self._camera.set_white_balance(WhiteBalanceMode.OFF)

            if success:
                mode_str = ["连续", "一次", "关闭"][mode] if mode < 3 else "未知"
                logger.info(f"白平衡设置成功: 模式={mode_str}")
                return ProtocolBuilder.build_success_response(frame.command)
            else:
                logger.warning("白平衡设置失败: 参数超出范围或相机不支持")
                return ProtocolBuilder.build_error_response(
                    frame.command, error_code or ErrorCode.CAMERA_PARAM_FAILED
                )

        except Exception as e:
            logger.error(f"设置白平衡异常: {e}")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.CAMERA_PARAM_FAILED
            )

    async def _handle_set_gain(self, client: ClientInfo, frame: ProtocolFrame) -> Optional[bytes]:
        """
        处理增益设置命令(0x22)

        数据格式: [增益值(2字节)]
        - 范围: 0-1000（映射到相机实际增益范围），大端序

        Args:
            client: 客户端信息
            frame: 协议帧

        Returns:
            Optional[bytes]: 响应数据
        """
        #检查相机是否连接
        if self._camera is None or not self._camera.is_connected:
            logger.warning("设置增益失败: 相机未连接")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.CAMERA_NOT_CONNECTED
            )

        #检查数据长度
        if len(frame.data) < 2:
            logger.warning(f"设置增益失败: 数据长度不足，期望2字节，实际{len(frame.data)}字节")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.DATA_LENGTH_ERROR
            )

        try:
            #解析数据
            gain_value = struct.unpack('>H', frame.data[0:2])[0]  #大端序2字节

            #获取相机增益范围并映射
            min_gain, max_gain = self._camera.get_gain_range()
            if min_gain == 0 and max_gain == 0:
                #无法获取增益范围，使用默认映射
                actual_gain = gain_value / 100.0  #0-1000映射到0-10
            else:
                #将0-1000映射到相机实际增益范围
                actual_gain = min_gain + (gain_value / 1000.0) * (max_gain - min_gain)

            logger.info(f"设置增益: 协议值={gain_value}, 实际值={actual_gain:.2f}")

            success, error_code = self._camera.set_gain(actual_gain)

            if success:
                logger.info(f"增益设置成功: {actual_gain:.2f}")
                return ProtocolBuilder.build_success_response(frame.command)
            else:
                logger.warning("增益设置失败: 参数超出范围")
                return ProtocolBuilder.build_error_response(
                    frame.command, error_code or ErrorCode.CAMERA_PARAM_FAILED
                )

        except Exception as e:
            logger.error(f"设置增益异常: {e}")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.CAMERA_PARAM_FAILED
            )

    async def _handle_set_gain_auto(self, client: ClientInfo, frame: ProtocolFrame) -> Optional[bytes]:
        """
        处理自动增益设置命令(0x24)

        数据格式: [模式(1字节)]
        - 模式: 0-关闭, 1-开启

        Args:
            client: 客户端信息
            frame: 协议帧

        Returns:
            Optional[bytes]: 响应数据
        """
        if self._camera is None or not self._camera.is_connected:
            logger.warning("设置自动增益失败: 相机未连接")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.CAMERA_NOT_CONNECTED
            )

        if len(frame.data) < 1:
            logger.warning(f"设置自动增益失败: 数据长度不足，期望1字节，实际{len(frame.data)}字节")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.DATA_LENGTH_ERROR
            )

        try:
            enabled = frame.data[0] == 1
            logger.info(f"设置自动增益: {'开启' if enabled else '关闭'}")
            success, error_code = self._camera.set_gain_auto(enabled)

            if success:
                return ProtocolBuilder.build_success_response(frame.command)
            return ProtocolBuilder.build_error_response(
                frame.command, error_code or ErrorCode.CAMERA_PARAM_FAILED
            )
        except Exception as e:
            logger.error(f"设置自动增益异常: {e}")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.CAMERA_PARAM_FAILED
            )

    async def _handle_set_frame_rate(self, client: ClientInfo, frame: ProtocolFrame) -> Optional[bytes]:
        """
        处理帧率设置命令(0x25)

        数据格式: [启用1字节][帧率4字节]
        - 启用: 0-关闭, 1-开启
        - 帧率: fps*100，4字节大端序
        """
        if self._camera is None or not self._camera.is_connected:
            logger.warning("设置帧率失败: 相机未连接")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.CAMERA_NOT_CONNECTED
            )

        if len(frame.data) < 5:
            logger.warning(f"设置帧率失败: 数据长度不足，期望5字节，实际{len(frame.data)}字节")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.DATA_LENGTH_ERROR
            )

        try:
            enable = frame.data[0] == 1
            fps_value = struct.unpack('>I', frame.data[1:5])[0] / 100.0
            logger.info(f"设置帧率: enable={enable}, fps={fps_value:.2f}")

            success, error_code = self._camera.set_frame_rate(fps_value, enable)
            if success:
                return ProtocolBuilder.build_success_response(frame.command)
            return ProtocolBuilder.build_error_response(
                frame.command, error_code or ErrorCode.CAMERA_PARAM_FAILED
            )
        except Exception as e:
            logger.error(f"设置帧率异常: {e}")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.CAMERA_PARAM_FAILED
            )

    async def _handle_set_pixel_format(self, client: ClientInfo, frame: ProtocolFrame) -> Optional[bytes]:
        """
        处理像素格式设置命令(0x26)

        数据格式: [格式索引1字节]
        """
        if self._camera is None or not self._camera.is_connected:
            logger.warning("设置像素格式失败: 相机未连接")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.CAMERA_NOT_CONNECTED
            )

        if len(frame.data) < 1:
            logger.warning(f"设置像素格式失败: 数据长度不足，期望1字节，实际{len(frame.data)}字节")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.DATA_LENGTH_ERROR
            )

        try:
            format_index = frame.data[0]
            if format_index not in self.PIXEL_FORMAT_MAP:
                logger.warning(f"未知像素格式索引: {format_index}")
                return ProtocolBuilder.build_error_response(
                    frame.command, ErrorCode.CAMERA_PARAM_FAILED
                )

            format_name = self.PIXEL_FORMAT_MAP[format_index]
            logger.info(f"设置像素格式: index={format_index}, name={format_name}")
            success, error_code = self._camera.set_pixel_format(format_name)
            if success:
                return ProtocolBuilder.build_success_response(frame.command)
            return ProtocolBuilder.build_error_response(
                frame.command, error_code or ErrorCode.CAMERA_PARAM_FAILED
            )
        except Exception as e:
            logger.error(f"设置像素格式异常: {e}")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.CAMERA_PARAM_FAILED
            )

    async def _handle_set_flash(self, client: ClientInfo, frame: ProtocolFrame) -> Optional[bytes]:
        """
        处理闪光灯设置命令(0x27)

        新格式: [启用1字节][延时4字节(ms, 大端)]
        - 启用: 0-关闭, 1-开启
        - 延时: 先发送闪光TCP触发，再等待该时长后触发相机拍照

        兼容旧格式(13字节): [启用1字节][延时4字节us][脉宽4字节][间隔4字节]
        - 旧格式的脉宽/间隔字段会被忽略，仅将delay_us转换为delay_ms。
        """
        if len(frame.data) not in (5, 13):
            logger.warning(
                f"设置闪光灯失败: 数据长度非法，期望5(新协议)或13(兼容旧协议)，实际{len(frame.data)}字节"
            )
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.DATA_LENGTH_ERROR
            )

        try:
            enable = frame.data[0] == 1
            if len(frame.data) == 5:
                delay_ms = struct.unpack('>i', frame.data[1:5])[0]
            else:
                delay_us = struct.unpack('>I', frame.data[1:5])[0]
                delay_ms = int(delay_us / 1000)
                logger.warning(
                    "收到旧版闪光灯协议(13字节)，已按delay_us转换为delay_ms；脉宽/间隔字段已忽略"
                )

            if delay_ms < 0:
                logger.warning(f"闪光延时不支持负值，已按0处理: {delay_ms}ms")
                delay_ms = 0

            # 先缓存用户配置。录像/预览期间只缓存，退出后恢复。
            self._flash_user_config = {
                "enable": bool(enable),
                "delay_ms": int(delay_ms),
            }

            logger.info(
                f"设置闪光灯(TCP触发): enable={enable}, delay={delay_ms}ms, "
                f"target={self.FLASH_TRIGGER_HOST}:{self.FLASH_TRIGGER_PORT}, payload=AA AA"
            )
            if enable and delay_ms >= int(self.CONTINUOUS_INTERVAL_SEC * 1000):
                logger.warning(
                    "闪光延时大于等于连拍周期，连拍时可能出现跨周期交错触发"
                )

            if self._is_streaming_active():
                logger.info("当前处于录像/预览中，闪光灯配置已缓存，将在退出后恢复")
                return ProtocolBuilder.build_success_response(frame.command)

            self._flash_forced_disabled = False
            return ProtocolBuilder.build_success_response(frame.command)
        except Exception as e:
            logger.error(f"设置闪光灯异常: {e}")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.CAMERA_PARAM_FAILED
            )

    async def _handle_set_resolution(self, client: ClientInfo, frame: ProtocolFrame) -> Optional[bytes]:
        """
        处理分辨率设置命令(0x23)

        数据格式: [宽(2字节)][高(2字节)]
        - 大端序

        Args:
            client: 客户端信息
            frame: 协议帧

        Returns:
            Optional[bytes]: 响应数据
        """
        #检查相机是否连接
        if self._camera is None or not self._camera.is_connected:
            logger.warning("设置分辨率失败: 相机未连接")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.CAMERA_NOT_CONNECTED
            )

        #检查数据长度
        if len(frame.data) < 4:
            logger.warning(f"设置分辨率失败: 数据长度不足，期望4字节，实际{len(frame.data)}字节")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.DATA_LENGTH_ERROR
            )

        try:
            #解析数据
            width = struct.unpack('>H', frame.data[0:2])[0]   #大端序2字节
            height = struct.unpack('>H', frame.data[2:4])[0]  #大端序2字节

            logger.info(f"设置分辨率: {width}x{height}")

            #验证分辨率是否在支持列表中
            supported = self._camera.get_supported_resolutions()
            if supported and (width, height) not in supported:
                #检查是否在最大范围内（允许自定义分辨率）
                max_res = supported[0] if supported else (0, 0)
                if width > max_res[0] or height > max_res[1]:
                    logger.warning(f"不支持的分辨率: {width}x{height}")
                    return ProtocolBuilder.build_error_response(
                        frame.command, ErrorCode.CAMERA_UNSUPPORTED_RES
                    )

            success, error_code = self._camera.set_resolution(width, height)

            if success:
                logger.info(f"分辨率设置成功: {width}x{height}")
                return ProtocolBuilder.build_success_response(frame.command)
            else:
                logger.warning(f"分辨率设置失败: {width}x{height}")
                return ProtocolBuilder.build_error_response(
                    frame.command, error_code or ErrorCode.CAMERA_UNSUPPORTED_RES
                )

        except Exception as e:
            logger.error(f"设置分辨率异常: {e}")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.CAMERA_PARAM_FAILED
            )

    #========== 状态构建辅助方法 ==========

    def _build_status_byte(self) -> int:
        """
        构建状态字节

        状态字节结构:
        - bit 0: 相机连接状态 (1=已连接)
        - bit 1: 正在拍照 (1=是)
        - bit 2: 正在录像 (1=是)
        - bit 3: 正在预览 (1=是)
        - bit 4: 正在连续拍照 (1=是)
        - bit 5-7: 保留

        Returns:
            int: 状态字节
        """
        status = 0

        #bit 0: 相机连接状态
        if self._camera and self._camera.is_connected:
            status |= 0x01

        #bit 1: 正在拍照
        if self._is_capturing:
            status |= 0x02

        #bit 2: 正在录像
        if self._is_recording:
            status |= 0x04

        #bit 3: 正在预览
        if self._is_previewing:
            status |= 0x08

        #bit 4: 正在连续拍照
        if self._is_continuous:
            status |= 0x10

        return status

    def _build_params_data(self) -> bytes:
        """
        构建参数数据

        参数结构体(共22字节，前18字节保持兼容):
        - 曝光模式(1字节): 0-自动, 1-手动
        - 曝光值(4字节): 微秒，大端序
        - 增益(2字节): 大端序（乘以100后取整）
        - 白平衡模式(1字节): 0-自动, 1-手动
        - 分辨率宽(2字节): 大端序
        - 分辨率高(2字节): 大端序
        - 相机实际帧率(4字节): fps*100，大端序

        Returns:
            bytes: 参数数据
        """
        #默认值
        exposure_mode = 1      #手动
        exposure_us = 10000    #10ms
        gain = 100             #1.0 * 100
        wb_mode = 0            #连续
        width = 1920
        height = 1080
        cam_fps_x100 = 0

        #从相机获取实际参数
        if self._camera and self._camera.is_connected:
            params = self._camera.get_parameters()
            if params:
                #曝光模式: "Off"=手动, "Continuous"=自动
                exposure_mode = 0 if params.exposure_mode == "Continuous" else 1
                exposure_us = int(params.exposure_time)
                #增益值映射：将相机实际增益值映射回协议值(0-1000)
                try:
                    min_gain, max_gain = self._camera.get_gain_range()
                    if min_gain is not None and max_gain is not None and max_gain > min_gain:
                        #将实际增益值映射到0-1000范围
                        gain = int((params.gain - min_gain) / (max_gain - min_gain) * 1000)
                    else:
                        #无法获取范围时，假设相机增益范围是0-10，直接乘以100
                        gain = int(params.gain * 100)
                except Exception as e:
                    logger.debug(f"增益值映射失败: {e}, 使用默认映射")
                    gain = int(params.gain * 100)

                #白平衡模式: 0-连续(Continuous), 1-一次(Once), 2-关闭(Off)
                if params.white_balance_mode == "Continuous":
                    wb_mode = 0
                elif params.white_balance_mode == "Once":
                    wb_mode = 1
                else:  # Off 或其他
                    wb_mode = 2

                #分辨率
                width = params.width
                height = params.height

            #读取相机当前实际输出帧率（Resulting FPS）
            try:
                if hasattr(self._camera, "get_resulting_frame_rate"):
                    cam_fps = float(self._camera.get_resulting_frame_rate())
                    if cam_fps > 0:
                        cam_fps_x100 = int(cam_fps * 100)
            except Exception as e:
                logger.debug(f"获取相机实际帧率失败: {e}")



        #打包数据（注意：曝光值用I是4字节无符号整数）
        try:
            data = struct.pack(
                '>BIHBHHHI',
                int(exposure_mode) & 0xFF,                        #曝光模式(1字节)
                max(0, min(0xFFFFFFFF, int(exposure_us))),        #曝光值(4字节，大端序)
                max(0, min(0xFFFF, int(gain))),                   #增益(2字节，大端序)
                int(wb_mode) & 0xFF,                              #白平衡模式(1字节)
                max(0, min(0xFFFF, int(width))),                  #分辨率宽(2字节)
                max(0, min(0xFFFF, int(height))),                 #分辨率高(2字节)
                max(0, min(0xFFFFFFFF, int(cam_fps_x100)))        #相机实际帧率*100(4字节)
            )
            return data
        except struct.error as e:
            logger.error(f"构建参数数据打包失败，回退默认参数: {e}")
            return struct.pack('>BIHBHHHI', 1, 10000, 100, 0, 1920, 1080, 0)

    def _get_supported_resolutions(self) -> list:
        """
        获取支持的分辨率列表

        Returns:
            list: 分辨率列表[(宽, 高), ...]
        """
        #默认分辨率列表
        default_resolutions = [
            (1920, 1080),
            (1280, 720),
            (640, 480),
        ]

        if self._camera and self._camera.is_connected:
            resolutions = self._camera.get_supported_resolutions()
            if resolutions:
                return resolutions

        return default_resolutions

    #========== 状态设置方法 ==========

    def set_capturing(self, capturing: bool) -> None:
        """设置拍照状态"""
        self._is_capturing = capturing

    def set_recording(self, recording: bool) -> None:
        """设置录像状态"""
        self._is_recording = recording

    def set_previewing(self, previewing: bool) -> None:
        """设置预览状态"""
        self._is_previewing = previewing

    def _is_streaming_active(self) -> bool:
        """当前是否处于录像或预览"""
        return self._is_recording or self._is_previewing

    def _is_flash_output_enabled(self) -> bool:
        """当前是否允许发送闪光TCP触发（受用户配置和录像/预览强制禁用状态影响）"""
        if self._flash_forced_disabled:
            return False
        return bool(self._flash_user_config.get("enable", False))

    def _send_flash_trigger(self, scene: str) -> bool:
        """
        通过TCP发送闪光触发（AA AA）
        """
        if not self._is_flash_output_enabled():
            logger.debug(f"跳过闪光触发（当前禁用）: {scene}")
            return False

        try:
            with self._flash_trigger_lock:
                with socket.create_connection(
                    (self.FLASH_TRIGGER_HOST, self.FLASH_TRIGGER_PORT),
                    timeout=self.FLASH_TRIGGER_TIMEOUT_SEC
                ) as sock:
                    sock.sendall(self.FLASH_TRIGGER_PAYLOAD)
            logger.info(
                f"闪光触发已发送: target={self.FLASH_TRIGGER_HOST}:{self.FLASH_TRIGGER_PORT}, "
                f"payload=AA AA, scene={scene}"
            )
            return True
        except Exception as e:
            logger.warning(
                f"闪光触发发送失败: target={self.FLASH_TRIGGER_HOST}:{self.FLASH_TRIGGER_PORT}, "
                f"scene={scene}, error={e}"
            )
            return False

    def _capture_single_with_flash_timing(
        self,
        scene: str,
        stop_event: Optional[threading.Event] = None
    ) -> tuple[Optional[Any], Optional[int], float, str, bool]:
        """
        执行一次“相机+闪光”时序触发并抓图。

        Returns:
            (image_array, error_code, lead_ts, lead_source, cancelled)
            lead_source: "camera" 或 "flash"
        """
        lead_ts = time.monotonic()
        delay_ms = max(0, int(self._flash_user_config.get("delay_ms", 0)))
        flash_enabled = self._is_flash_output_enabled()

        if flash_enabled:
            logger.info(
                f"{scene}: 先发送闪光TCP触发，再延时{delay_ms}ms后拍照"
            )
            self._send_flash_trigger(f"{scene}-lead-flash")
            wait_sec = delay_ms / 1000.0
            if wait_sec > 0:
                if stop_event is not None and stop_event.wait(timeout=wait_sec):
                    return None, None, lead_ts, "flash", True
                if stop_event is None:
                    time.sleep(wait_sec)
        else:
            logger.debug(f"{scene}: 相机直接抓图(闪光未启用)")

        image_array, error_code = self._camera.grab_single()
        return image_array, error_code, lead_ts, "flash" if flash_enabled else "camera", False

    def _suspend_flash_for_streaming(self, scene: str) -> tuple[bool, Optional[int]]:
        """
        在录像/预览开始前强制关闭闪光灯
        """
        if self._flash_forced_disabled:
            return True, None

        self._flash_forced_disabled = True
        logger.info(f"{scene}开始前已禁用闪光触发（TCP触发门禁）")
        return True, None

    def _restore_flash_after_streaming(self, scene: str) -> tuple[bool, Optional[int]]:
        """
        在录像/预览结束后恢复用户闪光灯配置
        """
        if self._is_streaming_active():
            return True, None

        if not self._flash_forced_disabled:
            return True, None

        self._flash_forced_disabled = False
        logger.info(
            f"{scene}结束后已恢复闪光触发状态: enable={self._flash_user_config.get('enable', False)}, "
            f"delay={self._flash_user_config.get('delay_ms', 0)}ms"
        )
        return True, None

    async def _send_to_client(self, client: ClientInfo, data: bytes):
        """
        发送数据到客户端

        Args:
            client: 客户端信息
            data: 要发送的数据
        """
        try:
            client.writer.write(data)
            await client.writer.drain()
        except Exception as e:
            logger.error(f"发送数据失败: {e}")

    async def _close_client(self, client_id: str, reason: str):
        """
        关闭客户端连接

        Args:
            client_id: 客户端ID
            reason: 关闭原因
        """
        if client_id not in self._clients:
            return

        client = self._clients.pop(client_id)

        #清理发送队列
        if client_id in self._send_queues:
            del self._send_queues[client_id]

        try:
            client.writer.close()
            await client.writer.wait_closed()
        except Exception as e:
            logger.debug(f"关闭客户端连接异常: {e}")

        logger.info(f"客户端 {client_id} 已断开: {reason}")

        #如果是控制者断开，选择新的控制者
        if client_id == self._controller_id:
            self._controller_id = None
            if self._clients:
                #选择第一个连接的客户端作为新控制者
                new_controller_id = next(iter(self._clients))
                self._controller_id = new_controller_id
                self._clients[new_controller_id].state = ClientState.CONTROLLING
                logger.info(f"新控制者: {new_controller_id}")

    async def _status_broadcast_loop(self):
        """状态广播循环"""
        while self._running:
            try:
                await asyncio.sleep(self._status_broadcast_interval)
                await self._broadcast_status()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"状态广播异常: {e}")

    async def _broadcast_status(self):
        """
        广播状态到所有客户端

        每1秒自动向所有连接的客户端广播0xA0状态上报
        状态字节结构:
        - bit 0: 相机连接状态 (1=已连接)
        - bit 1: 正在拍照 (1=是)
        - bit 2: 正在录像 (1=是)
        - bit 3: 正在预览 (1=是)
        - bit 4-7: 保留
        """
        if not self._clients:
            return

        #构建状态上报帧
        status_byte = self._build_status_byte()
        status_frame = ProtocolBuilder.build_status_report(bytes([status_byte]))

        #广播到所有客户端
        await self.broadcast(status_frame)

    async def broadcast(self, data: bytes):
        """
        广播数据到所有客户端

        Args:
            data: 要广播的数据
        """
        for client in self._clients.values():
            await self._send_to_client(client, data)

    async def send_to_controller(self, data: bytes):
        """
        发送数据到控制者

        Args:
            data: 要发送的数据
        """
        if self._controller_id and self._controller_id in self._clients:
            client = self._clients[self._controller_id]
            await self._send_to_client(client, data)

    @property
    def client_count(self) -> int:
        """获取当前连接的客户端数量"""
        return len(self._clients)

    @property
    def controller_id(self) -> Optional[str]:
        """获取当前控制者ID"""
        return self._controller_id

    @property
    def is_running(self) -> bool:
        """服务器是否运行中"""
        return self._running

    #========== 拍照控制处理器 ==========

    async def _handle_capture(self, client: ClientInfo, frame: ProtocolFrame) -> Optional[bytes]:
        """
        处理单次拍照命令(0x10)

        Args:
            client: 客户端信息
            frame: 协议帧

        Returns:
            Optional[bytes]: 响应数据
        """
        logger.info("收到拍照命令")

        #检查相机是否连接
        if self._camera is None or not self._camera.is_connected:
            logger.warning("拍照失败: 相机未连接")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.CAMERA_NOT_CONNECTED
            )

        #检查图像处理器
        if self._image_processor is None:
            logger.warning("拍照失败: 图像处理器未初始化")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.CAMERA_INIT_FAILED, "图像处理器未初始化"
            )

        #检查状态冲突
        if self._is_recording:
            logger.warning("拍照失败: 正在录像中")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.STATE_RECORDING
            )
        if self._is_capturing:
            logger.warning("拍照失败: 正在拍照中")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.STATE_CAPTURING
            )

        try:
            self._is_capturing = True
            #执行拍照
            logger.info("开始拍照...")
            image_array, error_code, _, _, cancelled = self._capture_single_with_flash_timing("单拍")
            if cancelled:
                logger.warning("拍照已取消")
                return ProtocolBuilder.build_error_response(
                    frame.command, ErrorCode.UNKNOWN_ERROR
                )

            if image_array is None:
                real_error = error_code or ErrorCode.CAMERA_GRAB_TIMEOUT
                logger.error(f"拍照失败: 错误码 0x{real_error:04X}")
                return ProtocolBuilder.build_error_response(
                    frame.command, real_error
                )

            #保存图像（使用numpy数组保存方法）
            success, result, save_error = self._image_processor.save_image_from_array(image_array)
            if success:
                logger.info(f"拍照成功: {result}")
                #发送拍照完成通知，result是文件路径
                filename = os.path.basename(result)
                return ProtocolBuilder.build_capture_complete(filename)
            else:
                logger.error(f"拍照失败: {result}")
                return ProtocolBuilder.build_error_response(
                    frame.command, save_error or ErrorCode.FILE_CREATE_FAILED
                )

        except Exception as e:
            logger.error(f"拍照异常: {e}")
            return self._build_exception_error_response(
                frame.command, e, default_code=ErrorCode.UNKNOWN_ERROR
            )
        finally:
            self._is_capturing = False

    #========== 录像控制处理器 ==========

    async def _handle_record_start(self, client: ClientInfo, frame: ProtocolFrame) -> Optional[bytes]:
        """
        处理开始录像命令(0x11)

        数据格式: [时长(4字节)][分辨率索引(1字节)][帧率(1字节)]
        - 时长: 秒，大端序，0表示手动停止
        - 分辨率索引: 0=5472x3648, 1=4096x2160, 2=3840x2160, 3=2736x1824,
                     4=1920x1080, 5=1280x720, 6=640x480
        - 帧率: 1-255（最终按相机能力范围裁剪）

        Args:
            client: 客户端信息
            frame: 协议帧

        Returns:
            Optional[bytes]: 响应数据
        """
        #检查相机是否连接
        if self._camera is None or not self._camera.is_connected:
            logger.warning("开始录像失败: 相机未连接")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.CAMERA_NOT_CONNECTED
            )

        #检查图像处理器和采集器
        if self._image_processor is None:
            logger.warning("开始录像失败: 图像处理器未初始化")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.CAMERA_INIT_FAILED, "图像处理器未初始化"
            )

        if self._image_acquisition is None:
            logger.warning("开始录像失败: 图像采集器未初始化")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.CAMERA_INIT_FAILED, "图像采集器未初始化"
            )

        #检查状态冲突
        if self._is_recording:
            logger.warning("开始录像失败: 已在录像中")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.STATE_RECORDING
            )

        if self._is_capturing:
            logger.warning("开始录像失败: 正在拍照中")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.STATE_CAPTURING
            )

        #检查数据长度
        if len(frame.data) < 6:
            logger.warning(f"开始录像失败: 数据长度不足，期望6字节，实际{len(frame.data)}字节")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.DATA_LENGTH_ERROR
            )

        try:
            #保存事件循环引用，供采集线程回调发送异步预览帧
            self._event_loop = asyncio.get_event_loop()

            #解析数据
            duration = struct.unpack('>I', frame.data[0:4])[0]  #时长（秒），大端序
            resolution_index = frame.data[4]                     #分辨率索引
            fps = frame.data[5]                                  #帧率

            #按协议范围与相机能力范围动态裁剪
            fps = self._normalize_fps(fps)
            resolution = self.RESOLUTION_MAP.get(resolution_index, (1920, 1080))

            logger.info(f"开始录像: 时长={duration}秒, 分辨率={resolution}, 帧率={fps}")

            #先下发ROI和帧率，避免仅使用请求值但相机侧未真正生效
            if self._camera and hasattr(self._camera, "set_resolution"):
                res_ok, res_err = self._camera.set_resolution(resolution[0], resolution[1])
                if not res_ok:
                    logger.warning("开始录像失败: 预设ROI分辨率失败")
                    return ProtocolBuilder.build_error_response(
                        frame.command, res_err or ErrorCode.CAMERA_PARAM_FAILED
                    )

            if self._camera and hasattr(self._camera, "set_frame_rate"):
                fps_ok, fps_err = self._camera.set_frame_rate(float(fps), True)
                if not fps_ok:
                    logger.warning("开始录像失败: 预设相机帧率失败")
                    return ProtocolBuilder.build_error_response(
                        frame.command, fps_err or ErrorCode.CAMERA_PARAM_FAILED
                    )

            # 录像期间强制关闭闪光灯，退出后自动恢复用户配置
            flash_ok, flash_err = self._suspend_flash_for_streaming("录像")
            if not flash_ok:
                logger.warning("开始录像失败: 禁用闪光灯失败")
                return ProtocolBuilder.build_error_response(
                    frame.command, flash_err or ErrorCode.CAMERA_PARAM_FAILED
                )

            #生成视频文件名
            video_filename = self._image_processor.generate_video_filename()

            #创建视频编码器
            success, error_code = self._image_processor.create_video_writer(
                video_filename, fps, resolution
            )

            if not success:
                logger.error(f"创建视频编码器失败: 错误码=0x{error_code:04X}")
                return ProtocolBuilder.build_error_response(
                    frame.command, error_code
                )

            #导入采集模式
            from image_acquisition import AcquisitionMode
            preview_resolution = self._get_record_preview_resolution(resolution)

            #定义帧回调函数
            def on_frame(image, frame_num):
                """帧回调：写入视频并实时回传预览帧"""
                if self._image_processor:
                    success, _ = self._image_processor.write_frame(image)
                    if not success:
                        logger.warning(f"写入视频帧失败: 帧号={frame_num}")

                #录像时也实时回传GUI预览帧
                self._send_record_preview_frame(image, frame_num, preview_resolution)

            #定义完成回调函数
            def on_complete():
                """录像完成回调"""
                if self._event_loop:
                    asyncio.run_coroutine_threadsafe(
                        self._on_recording_complete(),
                        self._event_loop
                    )

            #设置完成回调
            self._image_acquisition.set_complete_callback(on_complete)

            #启动连续采集
            success = self._image_acquisition.start_continuous(
                fps=fps,
                callback=on_frame,
                mode=AcquisitionMode.RECORDING,
                duration=duration,
                resolution_index=resolution_index
            )

            if not success:
                logger.error("启动连续采集失败")
                self._image_processor.close_video_writer()
                restore_ok, restore_err = self._restore_flash_after_streaming("录像")
                if not restore_ok:
                    logger.warning(f"录像启动失败后恢复闪光灯失败: 0x{(restore_err or ErrorCode.UNKNOWN_ERROR):04X}")
                return ProtocolBuilder.build_error_response(
                    frame.command, ErrorCode.CAMERA_GRAB_TIMEOUT
                )

            #更新状态
            self._is_recording = True

            logger.info(f"录像已开始: {video_filename}")
            return ProtocolBuilder.build_success_response(frame.command)

        except Exception as e:
            logger.error(f"开始录像异常: {e}")
            #清理资源
            if self._image_processor and self._image_processor.is_video_writing:
                self._image_processor.close_video_writer()
            restore_ok, restore_err = self._restore_flash_after_streaming("录像")
            if not restore_ok:
                logger.warning(f"录像异常后恢复闪光灯失败: 0x{(restore_err or ErrorCode.UNKNOWN_ERROR):04X}")
            return self._build_exception_error_response(
                frame.command, e, default_code=ErrorCode.UNKNOWN_ERROR
            )

    async def _handle_record_stop(self, client: ClientInfo, frame: ProtocolFrame) -> Optional[bytes]:
        """
        处理停止录像命令(0x12)

        数据格式: 无数据段

        Args:
            client: 客户端信息
            frame: 协议帧

        Returns:
            Optional[bytes]: 响应数据
        """
        #检查是否在录像中
        if not self._is_recording:
            logger.warning("停止录像失败: 未在录像中")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.UNKNOWN_ERROR
            )

        try:
            #停止采集
            if self._image_acquisition:
                self._image_acquisition.stop_continuous()

            #关闭视频编码器（会触发完成回调）
            #注意：完成回调会发送0xB1通知

            logger.info("录像停止命令已处理")
            return ProtocolBuilder.build_success_response(frame.command)

        except Exception as e:
            logger.error(f"停止录像异常: {e}")
            return self._build_exception_error_response(
                frame.command, e, default_code=ErrorCode.UNKNOWN_ERROR
            )

    async def _on_recording_complete(self):
        """
        录像完成处理

        关闭视频编码器并发送0xB1完成通知
        """
        try:
            #关闭视频编码器
            if self._image_processor:
                success, filepath = self._image_processor.close_video_writer()

                if success:
                    #提取文件名
                    filename = os.path.basename(filepath)

                    #发送录像完成通知(0xB1)
                    notify_frame = ProtocolBuilder.build_record_complete(filename)
                    await self.send_to_controller(notify_frame)

                    logger.info(f"录像完成通知已发送: {filename}")
                else:
                    logger.error(f"关闭视频编码器失败: {filepath}")

        except Exception as e:
            logger.error(f"录像完成处理异常: {e}")
        finally:
            #更新状态
            self._is_recording = False
            restore_ok, restore_err = self._restore_flash_after_streaming("录像")
            if not restore_ok:
                logger.warning(f"录像结束后恢复闪光灯失败: 0x{(restore_err or ErrorCode.UNKNOWN_ERROR):04X}")

    #========== 预览控制处理器 ==========

    async def _handle_preview_start(self, client: ClientInfo, frame: ProtocolFrame) -> Optional[bytes]:
        """
        处理开启实时预览命令(0x13)

        数据格式: [分辨率索引(1字节)][帧率(1字节)]
        - 分辨率索引: 0=5472x3648, 1=4096x2160, 2=3840x2160, 3=2736x1824,
                     4=1920x1080, 5=1280x720, 6=640x480
        - 帧率: 1-255（最终按相机能力范围裁剪）

        Args:
            client: 客户端信息
            frame: 协议帧

        Returns:
            Optional[bytes]: 响应数据
        """
        #检查相机是否连接
        if self._camera is None or not self._camera.is_connected:
            logger.warning("开启预览失败: 相机未连接")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.CAMERA_NOT_CONNECTED
            )

        #检查预览采集器
        if self._preview_acquisition is None:
            logger.warning("开启预览失败: 预览采集器未初始化")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.CAMERA_INIT_FAILED, "预览采集器未初始化"
            )

        #检查状态冲突
        if self._is_previewing:
            logger.warning("开启预览失败: 已在预览中")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.PREVIEW_ALREADY_STARTED
            )

        if self._is_recording:
            logger.warning("开启预览失败: 正在录像中")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.STATE_RECORDING
            )

        #检查数据长度
        if len(frame.data) < 2:
            logger.warning(f"开启预览失败: 数据长度不足，期望2字节，实际{len(frame.data)}字节")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.DATA_LENGTH_ERROR
            )

        try:
            #解析数据
            resolution_index = frame.data[0]  #分辨率索引
            fps = self._normalize_fps(frame.data[1])  #帧率（动态裁剪）
            resolution = self.RESOLUTION_MAP.get(resolution_index, (1920, 1080))

            logger.info(f"开启预览: 分辨率索引={resolution_index}, 帧率={fps}")

            #先下发ROI和帧率，确保预览链路按相机真实能力工作
            if self._camera and hasattr(self._camera, "set_resolution"):
                res_ok, res_err = self._camera.set_resolution(resolution[0], resolution[1])
                if not res_ok:
                    logger.warning("开启预览失败: 预设ROI分辨率失败")
                    return ProtocolBuilder.build_error_response(
                        frame.command, res_err or ErrorCode.CAMERA_PARAM_FAILED
                    )

            if self._camera and hasattr(self._camera, "set_frame_rate"):
                fps_ok, fps_err = self._camera.set_frame_rate(float(fps), True)
                if not fps_ok:
                    logger.warning("开启预览失败: 预设相机帧率失败")
                    return ProtocolBuilder.build_error_response(
                        frame.command, fps_err or ErrorCode.CAMERA_PARAM_FAILED
                    )

            # 预览期间强制关闭闪光灯，退出后自动恢复用户配置
            flash_ok, flash_err = self._suspend_flash_for_streaming("预览")
            if not flash_ok:
                logger.warning("开启预览失败: 禁用闪光灯失败")
                return ProtocolBuilder.build_error_response(
                    frame.command, flash_err or ErrorCode.CAMERA_PARAM_FAILED
                )

            #启动预览
            success, error_code = self._preview_acquisition.start_preview(
                resolution_index=resolution_index,
                fps=fps
            )

            if not success:
                logger.error(f"启动预览失败: 错误码=0x{error_code:04X}")
                restore_ok, restore_err = self._restore_flash_after_streaming("预览")
                if not restore_ok:
                    logger.warning(f"预览启动失败后恢复闪光灯失败: 0x{(restore_err or ErrorCode.UNKNOWN_ERROR):04X}")
                return ProtocolBuilder.build_error_response(
                    frame.command, error_code if error_code else ErrorCode.UNKNOWN_ERROR
                )

            #更新状态
            self._is_previewing = True

            logger.info("预览已开启")
            return ProtocolBuilder.build_success_response(frame.command)

        except Exception as e:
            logger.error(f"开启预览异常: {e}")
            restore_ok, restore_err = self._restore_flash_after_streaming("预览")
            if not restore_ok:
                logger.warning(f"预览异常后恢复闪光灯失败: 0x{(restore_err or ErrorCode.UNKNOWN_ERROR):04X}")
            return self._build_exception_error_response(
                frame.command, e, default_code=ErrorCode.UNKNOWN_ERROR
            )

    async def _handle_preview_stop(self, client: ClientInfo, frame: ProtocolFrame) -> Optional[bytes]:
        """
        处理停止实时预览命令(0x14)

        数据格式: 无数据段

        Args:
            client: 客户端信息
            frame: 协议帧

        Returns:
            Optional[bytes]: 响应数据
        """
        #检查是否在预览中
        if not self._is_previewing:
            logger.warning("停止预览失败: 未在预览中")
            #即使未在预览中也返回成功，保持幂等性
            return ProtocolBuilder.build_success_response(frame.command)

        try:
            #停止预览
            if self._preview_acquisition:
                success, error_code = self._preview_acquisition.stop_preview()

                if not success:
                    logger.warning(f"停止预览失败: 错误码=0x{error_code:04X}")
                    return ProtocolBuilder.build_error_response(
                        frame.command, error_code if error_code else ErrorCode.UNKNOWN_ERROR
                    )

            #更新状态
            self._is_previewing = False
            restore_ok, restore_err = self._restore_flash_after_streaming("预览")
            if not restore_ok:
                logger.warning(f"预览结束后恢复闪光灯失败: 0x{(restore_err or ErrorCode.UNKNOWN_ERROR):04X}")

            logger.info("预览已停止")
            return ProtocolBuilder.build_success_response(frame.command)

        except Exception as e:
            logger.error(f"停止预览异常: {e}")
            #即使异常也尝试更新状态
            self._is_previewing = False
            restore_ok, restore_err = self._restore_flash_after_streaming("预览")
            if not restore_ok:
                logger.warning(f"预览异常停止后恢复闪光灯失败: 0x{(restore_err or ErrorCode.UNKNOWN_ERROR):04X}")
            return self._build_exception_error_response(
                frame.command, e, default_code=ErrorCode.UNKNOWN_ERROR
            )

    def _encode_preview_jpeg(self, image, resolution: tuple) -> Optional[bytes]:
        """
        将采集图像编码为预览JPEG

        Args:
            image: 原始图像（numpy数组）
            resolution: 目标尺寸(宽, 高)

        Returns:
            Optional[bytes]: JPEG字节；失败返回None
        """
        if not OPENCV_AVAILABLE:
            return None

        try:
            frame = image

            if len(frame.shape) == 2:
                frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
            elif len(frame.shape) == 3 and frame.shape[2] == 4:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

            target_w, target_h = resolution
            if frame.shape[1] != target_w or frame.shape[0] != target_h:
                frame = cv2.resize(frame, (target_w, target_h), interpolation=cv2.INTER_LINEAR)

            ok, encoded = cv2.imencode(
                '.jpg',
                frame,
                [int(cv2.IMWRITE_JPEG_QUALITY), int(self.RECORD_PREVIEW_JPEG_QUALITY)]
            )
            if not ok:
                return None
            return encoded.tobytes()
        except Exception as e:
            logger.debug(f"录像预览JPEG编码失败: {e}")
            return None

    def _get_record_preview_resolution(self, source_resolution: tuple) -> tuple:
        """
        计算录像回传预览分辨率，降低GUI链路负载。

        保持长宽比，最长边不超过 RECORD_PREVIEW_MAX_EDGE。
        """
        src_w, src_h = source_resolution
        max_edge = max(1, int(self.RECORD_PREVIEW_MAX_EDGE))
        longest = max(src_w, src_h)
        if longest <= max_edge:
            return source_resolution

        scale = max_edge / float(longest)
        target_w = max(1, int(src_w * scale))
        target_h = max(1, int(src_h * scale))
        return (target_w, target_h)

    def _send_record_preview_frame(self, image, frame_num: int, resolution: tuple) -> None:
        """
        录像帧转预览JPEG并回传GUI

        Args:
            image: 原始图像（numpy数组）
            frame_num: 帧号
            resolution: 目标预览尺寸
        """
        if self._event_loop is None:
            return

        jpeg_data = self._encode_preview_jpeg(image, resolution)
        if not jpeg_data:
            return

        self._on_preview_frame(frame_num, jpeg_data, allow_recording=True)

    def _on_preview_frame(self, seq: int, jpeg_data: bytes, allow_recording: bool = False) -> None:
        """
        预览帧回调函数

        在预览采集线程中被调用，将帧数据发送到控制者

        Args:
            seq: 帧序号
            jpeg_data: JPEG图像数据
        """
        if not self._is_previewing and not allow_recording:
            return

        if self._event_loop is None:
            logger.warning("事件循环未初始化，无法发送预览帧")
            return

        try:
            #构建预览帧数据包(0xC0)
            preview_frame = ProtocolBuilder.build_preview_frame(seq, jpeg_data)

            #在事件循环中异步发送
            asyncio.run_coroutine_threadsafe(
                self._send_preview_frame(preview_frame),
                self._event_loop
            )

        except Exception as e:
            logger.error(f"发送预览帧失败: {e}")

    async def _send_preview_frame(self, frame_data: bytes) -> None:
        """
        异步发送预览帧到控制者

        Args:
            frame_data: 预览帧数据包
        """
        try:
            await self.send_to_controller(frame_data)
        except Exception as e:
            logger.error(f"发送预览帧异常: {e}")

    #========== 连续拍照处理 ==========

    async def _handle_continuous_start(self, client: ClientInfo, frame: ProtocolFrame) -> bytes:
        """处理开始连续拍照命令"""
        logger.info("收到开始连续拍照命令")

        #检查相机
        if self._camera is None or not self._camera.is_connected:
            logger.error("相机未连接")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.CAMERA_NOT_CONNECTED
            )

        #检查是否已在连续拍照
        if self._is_continuous:
            logger.warning("已在连续拍照中")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.STATE_RECORDING
            )

        #检查是否在录像
        if self._is_recording:
            logger.warning("正在录像，无法开始连续拍照")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.STATE_RECORDING
            )

        #保存事件循环引用
        self._event_loop = asyncio.get_event_loop()

        #启动连续拍照线程
        self._continuous_stop_event.clear()
        self._continuous_thread = threading.Thread(
            target=self._continuous_capture_loop,
            daemon=True,
            name="ContinuousCapture"
        )
        self._continuous_thread.start()
        self._is_continuous = True

        logger.info("连续拍照已开始")
        return ProtocolBuilder.build_success_response(frame.command)

    async def _handle_continuous_stop(self, client: ClientInfo, frame: ProtocolFrame) -> bytes:
        """处理停止连续拍照命令"""
        logger.info("收到停止连续拍照命令")

        if not self._is_continuous:
            logger.warning("未在连续拍照")
            return ProtocolBuilder.build_success_response(frame.command)

        #停止连续拍照线程
        self._continuous_stop_event.set()
        if self._continuous_thread and self._continuous_thread.is_alive():
            self._continuous_thread.join(timeout=3.0)
        self._is_continuous = False

        logger.info("连续拍照已停止")
        return ProtocolBuilder.build_success_response(frame.command)

    def _continuous_capture_loop(self):
        """连续拍照线程循环。连拍周期由主触发（先发送的触发）决定。"""
        logger.info("连续拍照线程启动")
        capture_count = 0

        while not self._continuous_stop_event.is_set():
            try:
                image_array, error_code, lead_ts, lead_source, cancelled = self._capture_single_with_flash_timing(
                    scene=f"连拍#{capture_count + 1}",
                    stop_event=self._continuous_stop_event
                )
                if cancelled:
                    break

                if image_array is None:
                    real_error = error_code or ErrorCode.CAMERA_GRAB_TIMEOUT
                    logger.error(f"连续拍照失败: 错误码 0x{real_error:04X}")
                else:
                    #保存图像
                    success, result, save_error = self._image_processor.save_image_from_array(image_array)
                    if success:
                        capture_count += 1
                        logger.info(f"连续拍照成功 [{capture_count}]: {result}")

                        #发送拍照完成通知
                        if self._event_loop:
                            filename = os.path.basename(result)
                            notify_frame = ProtocolBuilder.build_capture_complete(filename)
                            asyncio.run_coroutine_threadsafe(
                                self.send_to_controller(notify_frame),
                                self._event_loop
                            )
                    else:
                        logger.error(f"连续拍照保存失败: {result}")

                # 下一次周期由主触发时间点决定，不等待从触发完成
                next_cycle_ts = lead_ts + self.CONTINUOUS_INTERVAL_SEC
                remaining = next_cycle_ts - time.monotonic()
                if remaining > 0:
                    self._continuous_stop_event.wait(timeout=remaining)
                else:
                    logger.debug(
                        f"连拍周期已超时(主触发={lead_source})，立即进入下一周期，超时={-remaining:.3f}s"
                    )

            except Exception as e:
                logger.error(f"连续拍照异常: {e}")
                self._continuous_stop_event.wait(timeout=self.CONTINUOUS_INTERVAL_SEC)

        logger.info(f"连续拍照线程结束，共拍摄 {capture_count} 张")

    def set_continuous(self, continuous: bool) -> None:
        """设置连续拍照状态"""
        self._is_continuous = continuous


async def main():
    """测试入口"""
    #配置日志
    logger.add(
        "logs/tcp_server_{time}.log",
        rotation="10 MB",
        retention="10 days",
        level="DEBUG"
    )

    server = TCPServer(port=8899)

    try:
        await server.start()
    except KeyboardInterrupt:
        logger.info("收到中断信号")
    finally:
        await server.stop()


if __name__ == '__main__':
    asyncio.run(main())
