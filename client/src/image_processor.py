# -*- coding: utf-8 -*-
"""
图像处理模块

负责图像保存、文件命名、视频编码、预览图缩放和JPEG编码等功能
使用pypylon内置的PylonImage进行高质量JPEG保存
使用OpenCV进行视频编码
使用Pillow进行预览图缩放和快速JPEG编码

性能优化:
- 使用numpy vectorization替代循环
- 预分配缓冲区
- 避免不必要的数组复制
"""
import os
import io
import shutil
import threading
from datetime import datetime
from typing import Optional, Tuple

import numpy as np
from loguru import logger

try:
    import cv2
    OPENCV_AVAILABLE = True
except ImportError:
    OPENCV_AVAILABLE = False
    logger.warning("OpenCV未安装，视频编码功能不可用")

try:
    from pypylon import pylon
    PYPYLON_AVAILABLE = True
except ImportError:
    PYPYLON_AVAILABLE = False
    logger.warning("pypylon未安装，图像保存功能不可用")

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    logger.warning("Pillow未安装，预览图处理功能受限")


class ImageProcessor:
    """图像处理器"""

    #分辨率索引映射
    RESOLUTION_MAP = {
        0: (1920, 1080),
        1: (1280, 720),
        2: (640, 480),
    }

    def __init__(self, config_manager=None):
        """
        初始化图像处理器

        Args:
            config_manager: 配置管理器实例
        """
        self._config = config_manager
        self._lock = threading.Lock()

        #图像序号计数器（每日重置）
        self._sequence_counter = 0
        self._sequence_date = datetime.now().strftime("%Y%m%d")

        #默认配置
        self._save_path = "./images"
        self._video_path = "./videos"
        self._jpeg_quality = 95

        if config_manager:
            self._save_path = config_manager.storage_image_path
            self._jpeg_quality = config_manager.storage_jpeg_quality
            #视频保存路径（如果配置中有的话）
            if hasattr(config_manager, 'storage_video_path'):
                self._video_path = config_manager.storage_video_path

        #视频编码器相关
        self._video_writer: Optional[cv2.VideoWriter] = None
        self._video_filename: Optional[str] = None
        self._video_frame_count = 0
        self._video_fps = 5
        self._video_resolution = (1920, 1080)

        #确保保存目录存在
        self._ensure_save_dir()
        self._ensure_video_dir()

        logger.info(f"图像处理器初始化完成，图像路径: {self._save_path}, 视频路径: {self._video_path}")

    def _ensure_save_dir(self) -> bool:
        """
        确保保存目录存在

        Returns:
            bool: 目录是否可用
        """
        try:
            if not os.path.exists(self._save_path):
                os.makedirs(self._save_path)
                logger.info(f"创建图像保存目录: {self._save_path}")
            return True
        except Exception as e:
            logger.error(f"创建图像保存目录失败: {e}")
            return False

    def _ensure_video_dir(self) -> bool:
        """
        确保视频保存目录存在

        Returns:
            bool: 目录是否可用
        """
        try:
            if not os.path.exists(self._video_path):
                os.makedirs(self._video_path)
                logger.info(f"创建视频保存目录: {self._video_path}")
            return True
        except Exception as e:
            logger.error(f"创建视频保存目录失败: {e}")
            return False

    def _get_next_sequence(self) -> int:
        """
        获取下一个序号

        Returns:
            int: 序号
        """
        with self._lock:
            current_date = datetime.now().strftime("%Y%m%d")

            #日期变更时重置序号
            if current_date != self._sequence_date:
                self._sequence_date = current_date
                self._sequence_counter = 0

            self._sequence_counter += 1
            return self._sequence_counter

    def generate_filename(self, prefix: str = "IMG", extension: str = "jpg") -> str:
        """
        生成文件名

        格式: 时间戳_序号（如 20260121_153045_001.jpg）

        Args:
            prefix: 文件名前缀（未使用，保留接口兼容）
            extension: 文件扩展名

        Returns:
            str: 生成的文件名（不含路径）
        """
        now = datetime.now()
        timestamp = now.strftime("%Y%m%d_%H%M%S")
        seq = self._get_next_sequence()
        filename = f"{timestamp}_{seq:03d}.{extension}"
        return filename

    def get_full_path(self, filename: str) -> str:
        """
        获取完整文件路径

        Args:
            filename: 文件名

        Returns:
            str: 完整路径
        """
        return os.path.join(self._save_path, filename)

    def check_disk_space(self, required_mb: float = 100) -> Tuple[bool, float]:
        """
        检查磁盘空间

        Args:
            required_mb: 需要的最小空间（MB）

        Returns:
            Tuple[bool, float]: (空间是否充足, 可用空间MB)
        """
        try:
            stat = shutil.disk_usage(self._save_path)
            free_mb = stat.free / (1024 * 1024)
            return free_mb >= required_mb, free_mb
        except Exception as e:
            logger.error(f"检查磁盘空间失败: {e}")
            return False, 0

    def check_write_permission(self) -> bool:
        """
        检查写入权限

        Returns:
            bool: 是否有写入权限
        """
        try:
            test_file = os.path.join(self._save_path, ".write_test")
            with open(test_file, 'w') as f:
                f.write("test")
            os.remove(test_file)
            return True
        except Exception as e:
            logger.error(f"写入权限检查失败: {e}")
            return False

    def save_image(self, grab_result, filename: str = None) -> Tuple[bool, str, Optional[int]]:
        """
        保存图片

        使用pypylon内置的PylonImage.Save()保存JPEG

        Args:
            grab_result: pypylon的GrabResult对象
            filename: 文件名，为None时自动生成

        Returns:
            Tuple[bool, str, Optional[int]]: (是否成功, 文件路径或错误信息, 错误码或None)
        """
        if not PYPYLON_AVAILABLE:
            return False, "pypylon未安装", 0x0102

        #检查磁盘空间
        space_ok, free_mb = self.check_disk_space()
        if not space_ok:
            logger.error(f"磁盘空间不足: {free_mb:.1f}MB")
            return False, f"磁盘空间不足: {free_mb:.1f}MB", 0x0201

        #检查写入权限
        if not self.check_write_permission():
            logger.error("写入权限不足")
            return False, "写入权限不足", 0x0202

        #生成文件名
        if filename is None:
            filename = self.generate_filename()

        full_path = self.get_full_path(filename)

        try:
            #使用PylonImage保存
            img = pylon.PylonImage()
            img.AttachGrabResultBuffer(grab_result)

            #保存为JPEG
            img.Save(pylon.ImageFileFormat_Jpeg, full_path)
            img.Release()

            logger.info(f"图像保存成功: {full_path}")
            return True, full_path, None

        except Exception as e:
            logger.error(f"图像保存失败: {e}")
            return False, str(e), 0x0203

    def save_image_from_array(self, image_array, filename: str = None) -> Tuple[bool, str, Optional[int]]:
        """
        从numpy数组保存图片

        Args:
            image_array: numpy图像数组
            filename: 文件名，为None时自动生成

        Returns:
            Tuple[bool, str, Optional[int]]: (是否成功, 文件路径或错误信息, 错误码或None)
        """
        if not PYPYLON_AVAILABLE:
            return False, "pypylon未安装", 0x0102

        #检查磁盘空间
        space_ok, free_mb = self.check_disk_space()
        if not space_ok:
            logger.error(f"磁盘空间不足: {free_mb:.1f}MB")
            return False, f"磁盘空间不足: {free_mb:.1f}MB", 0x0201

        #检查写入权限
        if not self.check_write_permission():
            logger.error("写入权限不足")
            return False, "写入权限不足", 0x0202

        #生成文件名
        if filename is None:
            filename = self.generate_filename()

        full_path = self.get_full_path(filename)

        try:
            #使用PylonImage从数组创建并保存
            img = pylon.PylonImage()

            #根据数组维度判断像素格式
            if len(image_array.shape) == 2:
                #灰度图
                pixel_type = pylon.PixelType_Mono8
            elif len(image_array.shape) == 3 and image_array.shape[2] == 3:
                #BGR图像
                pixel_type = pylon.PixelType_BGR8packed
            else:
                logger.error(f"不支持的图像格式: {image_array.shape}")
                return False, "不支持的图像格式", 0x0501

            #创建PylonImage
            height, width = image_array.shape[:2]
            img.AttachUserBuffer(
                image_array.tobytes(),
                image_array.nbytes,
                pixel_type,
                width,
                height,
                0  #padding
            )

            #保存为JPEG
            img.Save(pylon.ImageFileFormat_Jpeg, full_path)
            img.Release()

            logger.info(f"图像保存成功: {full_path}")
            return True, full_path, None

        except Exception as e:
            logger.error(f"图像保存失败: {e}")
            return False, str(e), 0x0203

    def set_save_path(self, path: str) -> bool:
        """
        设置保存路径

        Args:
            path: 新的保存路径

        Returns:
            bool: 是否设置成功
        """
        old_path = self._save_path
        self._save_path = path

        if self._ensure_save_dir():
            logger.info(f"保存路径已更新: {path}")
            return True
        else:
            self._save_path = old_path
            return False

    def set_jpeg_quality(self, quality: int) -> None:
        """
        设置JPEG质量

        Args:
            quality: JPEG质量（1-100）
        """
        self._jpeg_quality = max(1, min(100, quality))
        logger.info(f"JPEG质量设置为: {self._jpeg_quality}")

    @property
    def save_path(self) -> str:
        """获取当前保存路径"""
        return self._save_path

    @property
    def jpeg_quality(self) -> int:
        """获取当前JPEG质量"""
        return self._jpeg_quality

    @property
    def video_path(self) -> str:
        """获取视频保存路径"""
        return self._video_path

    @property
    def is_video_writing(self) -> bool:
        """是否正在写入视频"""
        return self._video_writer is not None

    #========== 视频编码功能 ==========

    def create_video_writer(self, filename: str, fps: int, resolution: tuple) -> Tuple[bool, Optional[int]]:
        """
        创建视频编码器

        Args:
            filename: 视频文件名（不含路径）
            fps: 帧率
            resolution: 分辨率(宽, 高)

        Returns:
            Tuple[bool, Optional[int]]: (是否成功, 错误码或None)
        """
        if not OPENCV_AVAILABLE:
            logger.error("OpenCV未安装，无法创建视频编码器")
            return False, 0x0503

        with self._lock:
            #检查是否已有编码器在运行
            if self._video_writer is not None:
                logger.warning("视频编码器已在运行")
                return False, 0x0503

            #检查磁盘空间（视频需要更多空间）
            space_ok, free_mb = self.check_disk_space(500)  #至少500MB
            if not space_ok:
                logger.error(f"磁盘空间不足: {free_mb:.1f}MB")
                return False, 0x0201

            #确保视频目录存在
            if not self._ensure_video_dir():
                return False, 0x0202

            #生成完整路径
            full_path = os.path.join(self._video_path, filename)

            try:
                #尝试使用H.264编码器
                #优先尝试的编码器列表
                codecs = [
                    ('H264', 'mp4'),
                    ('avc1', 'mp4'),
                    ('X264', 'mp4'),
                    ('mp4v', 'mp4'),
                    ('XVID', 'avi'),
                ]

                width, height = resolution
                writer = None

                for codec, ext in codecs:
                    try:
                        fourcc = cv2.VideoWriter_fourcc(*codec)
                        #确保文件扩展名正确
                        if not full_path.endswith(f'.{ext}'):
                            test_path = full_path.rsplit('.', 1)[0] + f'.{ext}'
                        else:
                            test_path = full_path

                        writer = cv2.VideoWriter(test_path, fourcc, fps, (width, height))

                        if writer.isOpened():
                            full_path = test_path
                            logger.info(f"使用编码器: {codec}")
                            break
                        else:
                            writer.release()
                            writer = None
                    except Exception as e:
                        logger.debug(f"编码器 {codec} 不可用: {e}")
                        continue

                if writer is None or not writer.isOpened():
                    logger.error("无法创建视频编码器，所有编码器都不可用")
                    return False, 0x0503

                self._video_writer = writer
                self._video_filename = full_path
                self._video_frame_count = 0
                self._video_fps = fps
                self._video_resolution = resolution

                logger.info(f"视频编码器创建成功: {full_path}, {width}x{height}@{fps}fps")
                return True, None

            except Exception as e:
                logger.error(f"创建视频编码器失败: {e}")
                return False, 0x0503

    def write_frame(self, frame: np.ndarray) -> Tuple[bool, Optional[int]]:
        """
        写入视频帧

        Args:
            frame: 图像数据（numpy数组）

        Returns:
            Tuple[bool, Optional[int]]: (是否成功, 错误码或None)
        """
        if not OPENCV_AVAILABLE:
            return False, 0x0502

        with self._lock:
            if self._video_writer is None:
                logger.error("视频编码器未初始化")
                return False, 0x0503

            try:
                #检查图像格式并转换
                if len(frame.shape) == 2:
                    #灰度图转BGR
                    frame_bgr = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
                elif len(frame.shape) == 3:
                    if frame.shape[2] == 3:
                        #已经是BGR或RGB
                        frame_bgr = frame
                    elif frame.shape[2] == 4:
                        #BGRA转BGR
                        frame_bgr = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                    else:
                        logger.error(f"不支持的图像通道数: {frame.shape[2]}")
                        return False, 0x0502
                else:
                    logger.error(f"不支持的图像格式: {frame.shape}")
                    return False, 0x0502

                #调整分辨率（如果需要）
                target_width, target_height = self._video_resolution
                if frame_bgr.shape[1] != target_width or frame_bgr.shape[0] != target_height:
                    frame_bgr = cv2.resize(frame_bgr, (target_width, target_height))

                #写入帧
                self._video_writer.write(frame_bgr)
                self._video_frame_count += 1

                return True, None

            except Exception as e:
                logger.error(f"写入视频帧失败: {e}")
                return False, 0x0502

    def close_video_writer(self) -> Tuple[bool, str]:
        """
        关闭视频编码器

        Returns:
            Tuple[bool, str]: (是否成功, 文件路径或错误信息)
        """
        with self._lock:
            if self._video_writer is None:
                logger.warning("视频编码器未初始化")
                return False, "视频编码器未初始化"

            try:
                self._video_writer.release()
                self._video_writer = None

                filename = self._video_filename
                frame_count = self._video_frame_count

                #重置状态
                self._video_filename = None
                self._video_frame_count = 0

                logger.info(f"视频保存完成: {filename}, 总帧数: {frame_count}")
                return True, filename

            except Exception as e:
                logger.error(f"关闭视频编码器失败: {e}")
                self._video_writer = None
                return False, str(e)

    def generate_video_filename(self, extension: str = "mp4") -> str:
        """
        生成视频文件名

        格式: VID_时间戳_序号（如 VID_20260121_153045_001.mp4）

        Args:
            extension: 文件扩展名

        Returns:
            str: 生成的文件名（不含路径）
        """
        now = datetime.now()
        timestamp = now.strftime("%Y%m%d_%H%M%S")
        seq = self._get_next_sequence()
        filename = f"VID_{timestamp}_{seq:03d}.{extension}"
        return filename

    def get_video_statistics(self) -> dict:
        """
        获取视频编码统计信息

        Returns:
            dict: 统计信息
        """
        return {
            "is_writing": self._video_writer is not None,
            "filename": self._video_filename,
            "frame_count": self._video_frame_count,
            "fps": self._video_fps,
            "resolution": self._video_resolution,
        }


#========== 预览图处理辅助函数 ==========

def resize_preview_image(image: np.ndarray, target_width: int, target_height: int) -> np.ndarray:
    """
    缩放预览图像到目标尺寸

    使用Pillow进行高质量缩放，支持灰度图和彩色图

    Args:
        image: 原始图像（numpy数组，BGR或灰度）
        target_width: 目标宽度
        target_height: 目标高度

    Returns:
        np.ndarray: 缩放后的图像
    """
    try:
        #检查是否需要缩放
        h, w = image.shape[:2]
        if w == target_width and h == target_height:
            return image

        #优先使用Pillow
        if PIL_AVAILABLE:
            if len(image.shape) == 3:
                #彩色图像，BGR转RGB
                rgb_image = image[:, :, ::-1]
                pil_image = Image.fromarray(rgb_image)
            else:
                #灰度图像
                pil_image = Image.fromarray(image)

            #使用BILINEAR缩放（速度和质量的平衡）
            resized_pil = pil_image.resize((target_width, target_height), Image.BILINEAR)
            resized = np.array(resized_pil)

            #如果是彩色图像，RGB转回BGR
            if len(resized.shape) == 3:
                resized = resized[:, :, ::-1]

            return resized

        #备选：使用OpenCV
        elif OPENCV_AVAILABLE:
            return cv2.resize(image, (target_width, target_height), interpolation=cv2.INTER_LINEAR)

        else:
            logger.warning("Pillow和OpenCV都不可用，无法缩放图像")
            return image

    except Exception as e:
        logger.error(f"图像缩放失败: {e}")
        return image


def encode_preview_jpeg(image: np.ndarray, quality: int = 80) -> Optional[bytes]:
    """
    将numpy数组编码为JPEG字节流

    用于预览帧的快速JPEG编码

    Args:
        image: 图像数组（BGR或灰度）
        quality: JPEG质量（1-100），默认80

    Returns:
        Optional[bytes]: JPEG字节数据，失败返回None
    """
    try:
        #优先使用Pillow
        if PIL_AVAILABLE:
            #BGR转RGB（如果是彩色图像）
            if len(image.shape) == 3:
                rgb_image = image[:, :, ::-1]
            else:
                rgb_image = image

            #创建PIL图像
            pil_image = Image.fromarray(rgb_image)

            #编码为JPEG
            buffer = io.BytesIO()
            pil_image.save(buffer, format='JPEG', quality=quality)
            return buffer.getvalue()

        #备选：使用OpenCV
        elif OPENCV_AVAILABLE:
            encode_params = [cv2.IMWRITE_JPEG_QUALITY, quality]
            success, encoded = cv2.imencode('.jpg', image, encode_params)
            if success:
                return encoded.tobytes()
            else:
                logger.error("OpenCV JPEG编码失败")
                return None

        else:
            logger.error("Pillow和OpenCV都不可用，无法编码JPEG")
            return None

    except Exception as e:
        logger.error(f"JPEG编码失败: {e}")
        return None


def decode_jpeg_to_array(jpeg_data: bytes) -> Optional[np.ndarray]:
    """
    将JPEG字节流解码为numpy数组

    Args:
        jpeg_data: JPEG字节数据

    Returns:
        Optional[np.ndarray]: 图像数组（BGR格式），失败返回None
    """
    try:
        #优先使用Pillow
        if PIL_AVAILABLE:
            buffer = io.BytesIO(jpeg_data)
            pil_image = Image.open(buffer)
            rgb_array = np.array(pil_image)

            #RGB转BGR
            if len(rgb_array.shape) == 3:
                return rgb_array[:, :, ::-1]
            return rgb_array

        #备选：使用OpenCV
        elif OPENCV_AVAILABLE:
            nparr = np.frombuffer(jpeg_data, np.uint8)
            return cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        else:
            logger.error("Pillow和OpenCV都不可用，无法解码JPEG")
            return None

    except Exception as e:
        logger.error(f"JPEG解码失败: {e}")
        return None


#========== 性能优化：向量化图像处理函数 ==========

def fast_normalize(image: np.ndarray, out: np.ndarray = None) -> np.ndarray:
    """
    快速图像归一化（向量化操作）

    将图像像素值归一化到0-1范围

    Args:
        image: 输入图像（uint8）
        out: 输出缓冲区（float32）

    Returns:
        np.ndarray: 归一化后的图像
    """
    if out is None:
        return image.astype(np.float32) / 255.0
    else:
        np.divide(image, 255.0, out=out, casting='unsafe')
        return out


def fast_denormalize(image: np.ndarray, out: np.ndarray = None) -> np.ndarray:
    """
    快速图像反归一化（向量化操作）

    将0-1范围的图像转换回0-255

    Args:
        image: 输入图像（float32，0-1范围）
        out: 输出缓冲区（uint8）

    Returns:
        np.ndarray: 反归一化后的图像
    """
    if out is None:
        return (image * 255.0).astype(np.uint8)
    else:
        np.multiply(image, 255.0, out=out.astype(np.float32), casting='unsafe')
        return out.astype(np.uint8)


def fast_gamma_correction(image: np.ndarray, gamma: float = 1.0, out: np.ndarray = None) -> np.ndarray:
    """
    快速伽马校正（使用查找表优化）

    Args:
        image: 输入图像（uint8）
        gamma: 伽马值（>1变暗，<1变亮）
        out: 输出缓冲区

    Returns:
        np.ndarray: 校正后的图像
    """
    if gamma == 1.0:
        return image

    #构建查找表（256个值）
    inv_gamma = 1.0 / gamma
    lut = np.array([((i / 255.0) ** inv_gamma) * 255 for i in range(256)], dtype=np.uint8)

    #应用查找表（向量化操作）
    if out is None:
        return lut[image]
    else:
        np.take(lut, image, out=out)
        return out


def fast_histogram_equalization(image: np.ndarray) -> np.ndarray:
    """
    快速直方图均衡化（向量化操作）

    仅支持灰度图像

    Args:
        image: 输入灰度图像（uint8）

    Returns:
        np.ndarray: 均衡化后的图像
    """
    if len(image.shape) != 2:
        logger.warning("直方图均衡化仅支持灰度图像")
        return image

    #计算直方图
    hist, bins = np.histogram(image.flatten(), 256, [0, 256])

    #计算累积分布函数
    cdf = hist.cumsum()
    cdf_normalized = cdf * 255 / cdf[-1]

    #使用查找表应用均衡化
    return cdf_normalized[image].astype(np.uint8)


def fast_threshold(image: np.ndarray, threshold: int = 128, max_val: int = 255) -> np.ndarray:
    """
    快速二值化（向量化操作）

    Args:
        image: 输入图像
        threshold: 阈值
        max_val: 最大值

    Returns:
        np.ndarray: 二值化后的图像
    """
    return np.where(image > threshold, max_val, 0).astype(np.uint8)


def fast_blend(image1: np.ndarray, image2: np.ndarray, alpha: float = 0.5) -> np.ndarray:
    """
    快速图像混合（向量化操作）

    Args:
        image1: 第一张图像
        image2: 第二张图像
        alpha: 混合系数（0-1）

    Returns:
        np.ndarray: 混合后的图像
    """
    #使用numpy的加权平均
    return (image1 * alpha + image2 * (1 - alpha)).astype(np.uint8)


def fast_crop(image: np.ndarray, x: int, y: int, width: int, height: int) -> np.ndarray:
    """
    快速图像裁剪（使用切片，零拷贝）

    Args:
        image: 输入图像
        x: 起始x坐标
        y: 起始y坐标
        width: 裁剪宽度
        height: 裁剪高度

    Returns:
        np.ndarray: 裁剪后的图像（视图，非拷贝）
    """
    return image[y:y+height, x:x+width]


def fast_flip(image: np.ndarray, horizontal: bool = True) -> np.ndarray:
    """
    快速图像翻转（使用切片，零拷贝）

    Args:
        image: 输入图像
        horizontal: True为水平翻转，False为垂直翻转

    Returns:
        np.ndarray: 翻转后的图像（视图，非拷贝）
    """
    if horizontal:
        return image[:, ::-1]
    else:
        return image[::-1, :]


def fast_rotate_90(image: np.ndarray, clockwise: bool = True) -> np.ndarray:
    """
    快速90度旋转（使用numpy转置）

    Args:
        image: 输入图像
        clockwise: True为顺时针，False为逆时针

    Returns:
        np.ndarray: 旋转后的图像
    """
    if clockwise:
        return np.rot90(image, k=-1)
    else:
        return np.rot90(image, k=1)


class PreallocatedBuffer:
    """
    预分配缓冲区管理器

    用于减少频繁的内存分配开销
    """

    def __init__(self, shape: Tuple[int, ...], dtype=np.uint8, count: int = 2):
        """
        初始化缓冲区

        Args:
            shape: 缓冲区形状
            dtype: 数据类型
            count: 缓冲区数量（用于双缓冲）
        """
        self._buffers = [np.empty(shape, dtype=dtype) for _ in range(count)]
        self._current = 0
        self._shape = shape
        self._dtype = dtype
        self._lock = threading.Lock()

    def get_buffer(self) -> np.ndarray:
        """获取当前缓冲区"""
        with self._lock:
            buffer = self._buffers[self._current]
            self._current = (self._current + 1) % len(self._buffers)
            return buffer

    def resize(self, new_shape: Tuple[int, ...]) -> None:
        """调整缓冲区大小"""
        with self._lock:
            if new_shape != self._shape:
                self._shape = new_shape
                self._buffers = [np.empty(new_shape, dtype=self._dtype) for _ in range(len(self._buffers))]
                self._current = 0