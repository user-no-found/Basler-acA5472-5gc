#-*- coding: utf-8 -*-
"""
TCP客户端模块

功能:
- 连接到服务器
- 断开连接
- 发送协议帧
- 接收并解析响应
- 处理粘包/拆包
- 连接状态变化回调
- 数据接收回调

异常处理:
- 连接超时处理
- 连接断开检测
- 自动重连机制（可配置重连间隔和次数）
- 发送/接收超时处理
- 粘包/拆包异常处理
"""

import socket
import threading
import time
from typing import Callable, Optional, Tuple
from loguru import logger

from protocol_builder import (
    FRAME_HEADER, FRAME_FOOTER, PROTOCOL_VERSION,
    parse_frame, build_heartbeat, Command
)


class ConnectionState:
    """连接状态枚举"""
    DISCONNECTED = 0  #未连接
    CONNECTING = 1    #连接中
    CONNECTED = 2     #已连接
    RECONNECTING = 3  #重连中


class TcpClient:
    """TCP客户端类"""

    #默认配置
    DEFAULT_RECONNECT_INTERVAL = 5.0  #默认重连间隔（秒）
    DEFAULT_RECONNECT_MAX_ATTEMPTS = 10  #默认最大重连次数
    DEFAULT_HEARTBEAT_INTERVAL = 5.0  #默认心跳间隔（秒）
    DEFAULT_RECV_TIMEOUT = 1.0  #默认接收超时（秒）
    DEFAULT_SEND_TIMEOUT = 5.0  #默认发送超时（秒）

    def __init__(self):
        self._socket: Optional[socket.socket] = None
        self._state = ConnectionState.DISCONNECTED
        self._recv_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._running = False
        self._recv_buffer = bytearray()

        #回调函数
        self._on_state_changed: Optional[Callable[[int], None]] = None
        self._on_data_received: Optional[Callable[[int, int, bytes], None]] = None
        self._on_error: Optional[Callable[[str], None]] = None
        self._on_reconnect_failed: Optional[Callable[[], None]] = None  #重连失败回调

        #配置
        self._heartbeat_interval = self.DEFAULT_HEARTBEAT_INTERVAL
        self._recv_timeout = self.DEFAULT_RECV_TIMEOUT
        self._send_timeout = self.DEFAULT_SEND_TIMEOUT
        self._reconnect_enabled = False
        self._reconnect_interval = self.DEFAULT_RECONNECT_INTERVAL
        self._reconnect_max_attempts = self.DEFAULT_RECONNECT_MAX_ATTEMPTS
        self._reconnect_attempts = 0  #当前重连尝试次数

        #服务器地址
        self._host = ""
        self._port = 0

        #线程锁
        self._lock = threading.Lock()

        #统计信息
        self._bytes_sent = 0
        self._bytes_received = 0
        self._frames_sent = 0
        self._frames_received = 0
        self._last_heartbeat_time = 0.0
        self._heartbeat_timeout_count = 0  #心跳超时计数

    @property
    def state(self) -> int:
        """获取连接状态"""
        return self._state

    @property
    def is_connected(self) -> bool:
        """是否已连接"""
        return self._state == ConnectionState.CONNECTED

    def set_on_state_changed(self, callback: Callable[[int], None]):
        """设置连接状态变化回调"""
        self._on_state_changed = callback

    def set_on_data_received(self, callback: Callable[[int, int, bytes], None]):
        """
        设置数据接收回调

        Args:
            callback: 回调函数，参数为(版本号, 命令码, 数据段)
        """
        self._on_data_received = callback

    def set_on_error(self, callback: Callable[[str], None]):
        """设置错误回调"""
        self._on_error = callback

    def set_on_reconnect_failed(self, callback: Callable[[], None]):
        """设置重连失败回调"""
        self._on_reconnect_failed = callback

    def set_heartbeat_interval(self, interval: float):
        """设置心跳间隔（秒）"""
        self._heartbeat_interval = max(1.0, interval)

    def set_reconnect(self, enabled: bool, interval: float = 5.0, max_attempts: int = 10):
        """
        设置自动重连

        Args:
            enabled: 是否启用
            interval: 重连间隔（秒）
            max_attempts: 最大重连次数，0表示无限重试
        """
        self._reconnect_enabled = enabled
        self._reconnect_interval = max(1.0, interval)
        self._reconnect_max_attempts = max(0, max_attempts)

    def get_statistics(self) -> dict:
        """
        获取统计信息

        Returns:
            dict: 统计信息字典
        """
        return {
            "bytes_sent": self._bytes_sent,
            "bytes_received": self._bytes_received,
            "frames_sent": self._frames_sent,
            "frames_received": self._frames_received,
            "reconnect_attempts": self._reconnect_attempts,
            "heartbeat_timeout_count": self._heartbeat_timeout_count,
        }

    def connect(self, host: str, port: int, timeout: float = 5.0) -> bool:
        """
        连接到服务器

        Args:
            host: 服务器地址
            port: 服务器端口
            timeout: 连接超时（秒）

        Returns:
            是否连接成功
        """
        if self._state != ConnectionState.DISCONNECTED:
            logger.warning("已经连接或正在连接中")
            return False

        #验证参数
        if not host or not host.strip():
            logger.error("服务器地址不能为空")
            self._handle_error("服务器地址不能为空")
            return False

        if port < 1 or port > 65535:
            logger.error(f"端口号无效: {port}")
            self._handle_error(f"端口号无效: {port}")
            return False

        self._host = host.strip()
        self._port = port
        self._set_state(ConnectionState.CONNECTING)
        self._reconnect_attempts = 0  #重置重连计数

        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket.settimeout(timeout)
            self._socket.connect((self._host, port))
            self._socket.settimeout(self._recv_timeout)

            #禁用Nagle算法，减少延迟
            self._socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            #设置发送超时
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO,
                                    int(self._send_timeout * 1000).to_bytes(8, 'little'))

            self._running = True
            self._recv_buffer.clear()

            #重置统计
            self._bytes_sent = 0
            self._bytes_received = 0
            self._frames_sent = 0
            self._frames_received = 0
            self._heartbeat_timeout_count = 0

            #启动接收线程
            self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
            self._recv_thread.start()

            #启动心跳线程
            self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self._heartbeat_thread.start()

            self._set_state(ConnectionState.CONNECTED)
            logger.info(f"连接成功: {self._host}:{port}")
            return True

        except socket.timeout:
            error_msg = f"连接超时: {self._host}:{port}"
            logger.error(error_msg)
            self._handle_error(error_msg)
            self._cleanup()
            return False

        except socket.gaierror as e:
            error_msg = f"地址解析失败: {self._host}, 错误: {e}"
            logger.error(error_msg)
            self._handle_error(error_msg)
            self._cleanup()
            return False

        except ConnectionRefusedError:
            error_msg = f"连接被拒绝: {self._host}:{port}"
            logger.error(error_msg)
            self._handle_error(error_msg)
            self._cleanup()
            return False

        except socket.error as e:
            error_msg = f"连接失败: {self._host}:{port}, 错误: {e}"
            logger.error(error_msg)
            self._handle_error(error_msg)
            self._cleanup()
            return False

        except Exception as e:
            error_msg = f"连接异常: {e}"
            logger.error(error_msg)
            self._handle_error(error_msg)
            self._cleanup()
            return False

    def disconnect(self):
        """断开连接"""
        if self._state == ConnectionState.DISCONNECTED:
            return

        logger.info("断开连接")
        self._running = False
        self._reconnect_enabled = False  #禁用自动重连

        #关闭socket
        if self._socket:
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            except Exception as e:
                logger.debug(f"关闭socket异常: {e}")
            try:
                self._socket.close()
            except Exception as e:
                logger.debug(f"关闭socket异常: {e}")
            self._socket = None

        #等待线程结束
        if self._recv_thread and self._recv_thread.is_alive():
            self._recv_thread.join(timeout=2.0)
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=2.0)

        self._recv_thread = None
        self._heartbeat_thread = None
        self._recv_buffer.clear()

        self._set_state(ConnectionState.DISCONNECTED)

    def send(self, data: bytes) -> bool:
        """
        发送数据

        Args:
            data: 要发送的数据

        Returns:
            是否发送成功
        """
        if not self.is_connected:
            logger.warning("未连接，无法发送数据")
            return False

        with self._lock:
            try:
                self._socket.sendall(data)
                self._bytes_sent += len(data)
                self._frames_sent += 1
                logger.debug(f"发送数据: {data.hex().upper()}")
                return True
            except socket.timeout:
                error_msg = "发送超时"
                logger.error(error_msg)
                self._handle_error(error_msg)
                self._handle_disconnect()
                return False
            except BrokenPipeError:
                error_msg = "连接已断开（管道破裂）"
                logger.error(error_msg)
                self._handle_error(error_msg)
                self._handle_disconnect()
                return False
            except ConnectionResetError:
                error_msg = "连接被重置"
                logger.error(error_msg)
                self._handle_error(error_msg)
                self._handle_disconnect()
                return False
            except socket.error as e:
                error_msg = f"发送失败: {e}"
                logger.error(error_msg)
                self._handle_error(error_msg)
                self._handle_disconnect()
                return False
            except Exception as e:
                error_msg = f"发送异常: {e}"
                logger.error(error_msg)
                self._handle_error(error_msg)
                self._handle_disconnect()
                return False

    def send_heartbeat(self) -> bool:
        """发送心跳"""
        return self.send(build_heartbeat())

    def _set_state(self, state: int):
        """设置连接状态"""
        if self._state != state:
            old_state = self._state
            self._state = state
            logger.debug(f"状态变化: {old_state} -> {state}")
            if self._on_state_changed:
                try:
                    self._on_state_changed(state)
                except Exception as e:
                    logger.error(f"状态回调异常: {e}")

    def _handle_error(self, message: str):
        """处理错误"""
        if self._on_error:
            try:
                self._on_error(message)
            except Exception as e:
                logger.error(f"错误回调异常: {e}")

    def _handle_disconnect(self):
        """处理断开连接"""
        if self._state == ConnectionState.DISCONNECTED:
            return

        logger.warning("连接断开")
        self._cleanup()

        #自动重连
        if self._reconnect_enabled and self._host and self._port:
            threading.Thread(target=self._reconnect_loop, daemon=True).start()

    def _cleanup(self):
        """清理资源"""
        self._running = False
        if self._socket:
            try:
                self._socket.close()
            except:
                pass
            self._socket = None
        self._set_state(ConnectionState.DISCONNECTED)

    def _recv_loop(self):
        """接收数据循环"""
        logger.debug("接收线程启动")
        consecutive_errors = 0  #连续错误计数
        max_consecutive_errors = 5  #最大连续错误次数

        while self._running:
            try:
                data = self._socket.recv(4096)
                if not data:
                    #连接关闭
                    logger.warning("服务器关闭连接")
                    self._handle_disconnect()
                    break

                consecutive_errors = 0  #重置错误计数
                self._bytes_received += len(data)
                self._recv_buffer.extend(data)
                self._process_buffer()

            except socket.timeout:
                #接收超时，继续循环
                continue

            except ConnectionResetError:
                if self._running:
                    logger.error("连接被服务器重置")
                    self._handle_error("连接被服务器重置")
                    self._handle_disconnect()
                break

            except ConnectionAbortedError:
                if self._running:
                    logger.error("连接被中止")
                    self._handle_error("连接被中止")
                    self._handle_disconnect()
                break

            except socket.error as e:
                if self._running:
                    consecutive_errors += 1
                    logger.error(f"接收错误: {e}")
                    if consecutive_errors >= max_consecutive_errors:
                        logger.error(f"连续接收错误达到{max_consecutive_errors}次，断开连接")
                        self._handle_error(f"连续接收错误: {e}")
                        self._handle_disconnect()
                        break
                else:
                    break

            except Exception as e:
                if self._running:
                    logger.error(f"接收异常: {e}")
                    self._handle_error(f"接收异常: {e}")
                    self._handle_disconnect()
                break

        logger.debug("接收线程退出")

    def _process_buffer(self):
        """
        处理接收缓冲区，解析完整帧

        处理粘包/拆包问题，验证帧格式
        """
        #防止缓冲区过大（可能是协议错误导致）
        max_buffer_size = 10 * 1024 * 1024  #10MB（支持大预览帧）
        if len(self._recv_buffer) > max_buffer_size:
            logger.warning(f"接收缓冲区过大({len(self._recv_buffer)}字节)，清空缓冲区")
            self._recv_buffer.clear()
            self._handle_error("接收缓冲区溢出，可能存在协议错误")
            return

        while True:
            #查找帧头
            header_index = self._recv_buffer.find(FRAME_HEADER)
            if header_index == -1:
                #没有帧头，清空缓冲区
                if len(self._recv_buffer) > 0:
                    logger.debug(f"丢弃无效数据: {len(self._recv_buffer)}字节")
                self._recv_buffer.clear()
                break

            #丢弃帧头之前的数据
            if header_index > 0:
                logger.warning(f"丢弃无效数据: {self._recv_buffer[:header_index].hex().upper()}")
                del self._recv_buffer[:header_index]

            #检查是否有足够数据解析长度字段
            #帧头(2)+版本(1)+长度(4) = 7
            if len(self._recv_buffer) < 7:
                break

            #解析长度（大端序，4字节）
            length = (self._recv_buffer[3] << 24) | (self._recv_buffer[4] << 16) | \
                     (self._recv_buffer[5] << 8) | self._recv_buffer[6]

            #检查长度是否合理
            max_data_length = 10 * 1024 * 1024  #最大数据长度10MB
            if length > max_data_length:
                logger.warning(f"数据长度异常: {length}，丢弃帧头")
                del self._recv_buffer[:2]
                continue

            #计算完整帧长度
            #帧头(2)+版本(1)+长度(4)+命令(1)+数据(N-1)+校验(1)+帧尾(2)
            frame_length = 2 + 1 + 4 + length + 1 + 2

            #检查是否有完整帧
            if len(self._recv_buffer) < frame_length:
                break

            #检查帧尾
            if self._recv_buffer[frame_length-2:frame_length] != FRAME_FOOTER:
                #帧尾不匹配，丢弃帧头，继续查找
                logger.warning("帧尾不匹配，丢弃帧头")
                del self._recv_buffer[:2]
                continue

            #提取完整帧
            frame = bytes(self._recv_buffer[:frame_length])
            del self._recv_buffer[:frame_length]

            #解析帧
            result = parse_frame(frame)
            if result:
                version, cmd, data = result
                self._frames_received += 1
                logger.debug(f"收到帧: version=0x{version:02X}, cmd=0x{cmd:02X}, data_len={len(data)}")

                #调用回调
                if self._on_data_received:
                    try:
                        self._on_data_received(version, cmd, data)
                    except Exception as e:
                        logger.error(f"数据回调异常: {e}")
            else:
                logger.warning(f"帧解析失败（XOR校验错误）: {frame.hex().upper()}")
                self._handle_error("收到校验错误的数据帧")

    def _heartbeat_loop(self):
        """心跳发送循环"""
        logger.debug("心跳线程启动")
        while self._running:
            time.sleep(self._heartbeat_interval)
            if self._running and self.is_connected:
                self._last_heartbeat_time = time.time()
                if not self.send_heartbeat():
                    self._heartbeat_timeout_count += 1
                    logger.warning(f"心跳发送失败 (第{self._heartbeat_timeout_count}次)")
        logger.debug("心跳线程退出")

    def _reconnect_loop(self):
        """
        自动重连循环

        实现带最大重试次数的自动重连机制
        """
        logger.info(f"将在{self._reconnect_interval}秒后尝试重连")
        time.sleep(self._reconnect_interval)

        while self._reconnect_enabled and self._state == ConnectionState.DISCONNECTED:
            #检查重连次数
            if self._reconnect_max_attempts > 0 and self._reconnect_attempts >= self._reconnect_max_attempts:
                logger.error(f"重连失败，已达到最大重试次数({self._reconnect_max_attempts})")
                self._handle_error(f"重连失败，已尝试{self._reconnect_attempts}次")
                if self._on_reconnect_failed:
                    try:
                        self._on_reconnect_failed()
                    except Exception as e:
                        logger.error(f"重连失败回调异常: {e}")
                break

            self._reconnect_attempts += 1
            self._set_state(ConnectionState.RECONNECTING)
            logger.info(f"尝试重连: {self._host}:{self._port} (第{self._reconnect_attempts}次)")

            if self.connect(self._host, self._port):
                logger.info("重连成功")
                self._reconnect_attempts = 0
                break
            else:
                logger.warning(f"重连失败，{self._reconnect_interval}秒后重试")
                self._set_state(ConnectionState.DISCONNECTED)
                time.sleep(self._reconnect_interval)


if __name__ == '__main__':
    #测试代码
    import sys
    logger.remove()
    logger.add(sys.stdout, level="DEBUG")

    def on_state_changed(state):
        states = {0: "未连接", 1: "连接中", 2: "已连接"}
        print(f"状态变化: {states.get(state, '未知')}")

    def on_data_received(version, cmd, data):
        print(f"收到数据: version=0x{version:02X}, cmd=0x{cmd:02X}, data={data.hex()}")

    def on_error(message):
        print(f"错误: {message}")

    client = TcpClient()
    client.set_on_state_changed(on_state_changed)
    client.set_on_data_received(on_data_received)
    client.set_on_error(on_error)

    #测试连接（需要服务器运行）
    print("尝试连接到 127.0.0.1:8899...")
    if client.connect("127.0.0.1", 8899, timeout=3.0):
        print("连接成功，等待5秒...")
        time.sleep(5)
        client.disconnect()
    else:
        print("连接失败")
