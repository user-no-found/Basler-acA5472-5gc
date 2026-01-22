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
from typing import Dict, Optional, Callable, Awaitable, Any, TYPE_CHECKING
from dataclasses import dataclass, field
from enum import IntEnum
from datetime import datetime
from collections import deque

from loguru import logger

from .protocol_parser import (
    ProtocolParser,
    ProtocolBuilder,
    ProtocolFrame,
    CommandCode,
    check_version_compatible,
    PROTOCOL_VERSION,
)
from .utils.errors import ErrorCode, get_error_description

if TYPE_CHECKING:
    from .camera_controller import CameraController
    from .image_processor import ImageProcessor
    from .image_acquisition import ImageAcquisition, PreviewAcquisition


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

        #录像相关
        self._recording_task: Optional[asyncio.Task] = None
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None

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
        #注册录像控制处理器
        self.register_handler(CommandCode.RECORD_START, self._handle_record_start)
        self.register_handler(CommandCode.RECORD_STOP, self._handle_record_stop)
        #注册预览控制处理器
        self.register_handler(CommandCode.PREVIEW_START, self._handle_preview_start)
        self.register_handler(CommandCode.PREVIEW_STOP, self._handle_preview_stop)

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

        参数结构体(共18字节):
        - 曝光模式(1字节): 0-自动, 1-手动
        - 曝光值(4字节): 微秒，大端序
        - 增益(2字节): 大端序（乘以100后取整）
        - 白平衡模式(1字节): 0-自动, 1-手动
        - 白平衡R(2字节): 大端序（乘以100后取整）
        - 白平衡G(2字节): 大端序（固定100，即1.0）
        - 白平衡B(2字节): 大端序（乘以100后取整）
        - 分辨率宽(2字节): 大端序
        - 分辨率高(2字节): 大端序

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
            from .camera_controller import ExposureMode

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

        数据格式: [模式(1字节)][R(2字节)][G(2字节)][B(2字节)]
        - 模式: 0-自动, 1-手动
        - R/G/B: 白平衡比例值（0-1000映射到0.0-10.0），大端序

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
        if len(frame.data) < 7:
            logger.warning(f"设置白平衡失败: 数据长度不足，期望7字节，实际{len(frame.data)}字节")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.DATA_LENGTH_ERROR
            )

        try:
            #解析数据
            mode = frame.data[0]  #0-自动, 1-手动
            r_value = struct.unpack('>H', frame.data[1:3])[0]  #大端序2字节
            g_value = struct.unpack('>H', frame.data[3:5])[0]
            b_value = struct.unpack('>H', frame.data[5:7])[0]

            #将0-1000映射到0.0-10.0的比例值
            r_ratio = r_value / 100.0
            g_ratio = g_value / 100.0
            b_ratio = b_value / 100.0

            logger.info(f"设置白平衡: 模式={mode}, R={r_ratio:.2f}, G={g_ratio:.2f}, B={b_ratio:.2f}")

            #导入白平衡模式枚举
            from .camera_controller import WhiteBalanceMode

            if mode == 0:
                #自动白平衡
                success, error_code = self._camera.set_white_balance(WhiteBalanceMode.AUTO)
            else:
                #手动白平衡
                success, error_code = self._camera.set_white_balance(
                    WhiteBalanceMode.MANUAL,
                    red_ratio=r_ratio,
                    green_ratio=g_ratio,
                    blue_ratio=b_ratio
                )

            if success:
                logger.info(f"白平衡设置成功: 模式={'自动' if mode == 0 else '手动'}")
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
        - bit 4-7: 保留

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

        return status

    def _build_params_data(self) -> bytes:
        """
        构建参数数据

        参数结构体(共18字节):
        - 曝光模式(1字节): 0-自动, 1-手动
        - 曝光值(4字节): 微秒，大端序
        - 增益(2字节): 大端序（乘以100后取整）
        - 白平衡模式(1字节): 0-自动, 1-手动
        - 白平衡R(2字节): 大端序（乘以100后取整）
        - 白平衡G(2字节): 大端序（固定100，即1.0）
        - 白平衡B(2字节): 大端序（乘以100后取整）
        - 分辨率宽(2字节): 大端序
        - 分辨率高(2字节): 大端序

        Returns:
            bytes: 参数数据
        """
        #默认值
        exposure_mode = 1      #手动
        exposure_us = 10000    #10ms
        gain = 100             #1.0 * 100
        wb_mode = 0            #自动
        wb_r = 100             #1.0 * 100
        wb_g = 100             #1.0 * 100
        wb_b = 100             #1.0 * 100
        width = 1920
        height = 1080

        #从相机获取实际参数
        if self._camera and self._camera.is_connected:
            params = self._camera.get_parameters()
            if params:
                #曝光模式: "Off"=手动, "Continuous"=自动
                exposure_mode = 0 if params.exposure_mode == "Continuous" else 1
                exposure_us = int(params.exposure_time)
                gain = int(params.gain * 100)

                #白平衡模式
                wb_mode = 0 if params.white_balance_mode == "Continuous" else 1

                #分辨率
                width = params.width
                height = params.height

            #尝试获取白平衡值（需要从相机直接读取）
            try:
                if hasattr(self._camera, '_camera') and self._camera._camera:
                    cam = self._camera._camera
                    if hasattr(cam, 'BalanceRatioSelector'):
                        #读取红色通道
                        cam.BalanceRatioSelector.SetValue("Red")
                        wb_r = int(cam.BalanceRatio.Value * 100)
                        #读取蓝色通道
                        cam.BalanceRatioSelector.SetValue("Blue")
                        wb_b = int(cam.BalanceRatio.Value * 100)
            except Exception as e:
                logger.debug(f"获取白平衡值失败: {e}")

        #打包数据（注意：曝光值用I是4字节无符号整数）
        data = struct.pack(
            '>BIHBHHHHHH',
            exposure_mode,     #曝光模式(1字节)
            exposure_us,       #曝光值(4字节，大端序)
            gain,              #增益(2字节，大端序)
            wb_mode,           #白平衡模式(1字节)
            wb_r,              #白平衡R(2字节)
            wb_g,              #白平衡G(2字节)
            wb_b,              #白平衡B(2字节)
            width,             #分辨率宽(2字节)
            height             #分辨率高(2字节)
        )
        return data

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
                frame.command, ErrorCode.UNKNOWN_ERROR
            )

        #检查状态冲突
        if self._is_recording:
            logger.warning("拍照失败: 正在录像中")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.STATE_RECORDING
            )

        try:
            #执行拍照
            logger.info("开始拍照...")
            image_array, error_code = self._camera.grab_single()

            if image_array is None:
                logger.error(f"拍照失败: 错误码 0x{error_code:04X}")
                return ProtocolBuilder.build_error_response(
                    frame.command, error_code
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
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.UNKNOWN_ERROR
            )

    #========== 录像控制处理器 ==========

    async def _handle_record_start(self, client: ClientInfo, frame: ProtocolFrame) -> Optional[bytes]:
        """
        处理开始录像命令(0x11)

        数据格式: [时长(4字节)][分辨率索引(1字节)][帧率(1字节)]
        - 时长: 秒，大端序，0表示手动停止
        - 分辨率索引: 0=5472x3648, 1=4096x2160, 2=3840x2160, 3=2736x1824,
                     4=1920x1080, 5=1280x720, 6=640x480
        - 帧率: 1-30

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
                frame.command, ErrorCode.UNKNOWN_ERROR
            )

        if self._image_acquisition is None:
            logger.warning("开始录像失败: 图像采集器未初始化")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.UNKNOWN_ERROR
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
            #解析数据
            duration = struct.unpack('>I', frame.data[0:4])[0]  #时长（秒），大端序
            resolution_index = frame.data[4]                     #分辨率索引
            fps = frame.data[5]                                  #帧率

            #验证参数
            fps = max(1, min(30, fps))  #帧率范围1-30
            resolution = self.RESOLUTION_MAP.get(resolution_index, (1920, 1080))

            logger.info(f"开始录像: 时长={duration}秒, 分辨率={resolution}, 帧率={fps}")

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
            from .image_acquisition import AcquisitionMode

            #定义帧回调函数
            def on_frame(image, frame_num):
                """帧回调：写入视频"""
                if self._image_processor:
                    success, _ = self._image_processor.write_frame(image)
                    if not success:
                        logger.warning(f"写入视频帧失败: 帧号={frame_num}")

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
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.UNKNOWN_ERROR
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
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.UNKNOWN_ERROR
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

    #========== 预览控制处理器 ==========

    async def _handle_preview_start(self, client: ClientInfo, frame: ProtocolFrame) -> Optional[bytes]:
        """
        处理开启实时预览命令(0x13)

        数据格式: [分辨率索引(1字节)][帧率(1字节)]
        - 分辨率索引: 0=5472x3648, 1=4096x2160, 2=3840x2160, 3=2736x1824,
                     4=1920x1080, 5=1280x720, 6=640x480
        - 帧率: 5-30

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
                frame.command, ErrorCode.UNKNOWN_ERROR
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
            fps = frame.data[1]               #帧率

            logger.info(f"开启预览: 分辨率索引={resolution_index}, 帧率={fps}")

            #启动预览
            success, error_code = self._preview_acquisition.start_preview(
                resolution_index=resolution_index,
                fps=fps
            )

            if not success:
                logger.error(f"启动预览失败: 错误码=0x{error_code:04X}")
                return ProtocolBuilder.build_error_response(
                    frame.command, error_code if error_code else ErrorCode.UNKNOWN_ERROR
                )

            #更新状态
            self._is_previewing = True

            logger.info("预览已开启")
            return ProtocolBuilder.build_success_response(frame.command)

        except Exception as e:
            logger.error(f"开启预览异常: {e}")
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.UNKNOWN_ERROR
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

            logger.info("预览已停止")
            return ProtocolBuilder.build_success_response(frame.command)

        except Exception as e:
            logger.error(f"停止预览异常: {e}")
            #即使异常也尝试更新状态
            self._is_previewing = False
            return ProtocolBuilder.build_error_response(
                frame.command, ErrorCode.UNKNOWN_ERROR
            )

    def _on_preview_frame(self, seq: int, jpeg_data: bytes) -> None:
        """
        预览帧回调函数

        在预览采集线程中被调用，将帧数据发送到控制者

        Args:
            seq: 帧序号
            jpeg_data: JPEG图像数据
        """
        if not self._is_previewing:
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
