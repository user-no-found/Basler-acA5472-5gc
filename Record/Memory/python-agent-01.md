# Python Agent 记忆文件 - python-agent-01

## 基本信息
- **槽位**: python-agent-01
- **项目**: Basler 相机控制系统
- **创建时间**: 2026-01-21

## 已完成任务

### 任务 1.1: 客户端 - TCP 服务器基础
- **完成时间**: 2026-01-21
- **产物**:
  - `/mnt/d/desktop/Basler/client/src/tcp_server.py` - TCP 服务器模块
  - `/mnt/d/desktop/Basler/client/src/protocol_parser.py` - 协议解析模块
  - `/mnt/d/desktop/Basler/client/src/utils/xor_checksum.py` - XOR 校验工具
  - `/mnt/d/desktop/Basler/client/src/utils/errors.py` - 错误码定义
  - `/mnt/d/desktop/Basler/client/src/utils/__init__.py` - utils包初始化
  - `/mnt/d/desktop/Basler/client/src/__init__.py` - src包初始化

- **实现要点**:
  1. 基于 asyncio 实现异步 TCP 服务器，监听 8899 端口
  2. 协议帧结构：FE FE [版本号] [长度2字节] [命令码] [数据段] [XOR校验] EF EF
  3. 支持粘包/拆包处理，使用缓冲区循环查找完整帧
  4. XOR 异或校验（1字节），校验范围：版本号+长度+命令码+数据段
  5. 支持多客户端连接，但同时只有一个可控制
  6. 完整错误码定义（5大类20+错误码）
  7. 命令分发框架，支持注册自定义命令处理器

- **验收标准达成**:
  - [x] 可监听 8899 端口
  - [x] 正确解析帧头/帧尾/长度
  - [x] XOR 校验通过/失败正确处理
  - [x] 命令分发框架可用

### 任务 2.5: 客户端 - 状态上报
- **完成时间**: 2026-01-21
- **产物**:
  - 更新 `/mnt/d/desktop/Basler/client/src/tcp_server.py` - 添加状态查询和上报功能

- **实现要点**:
  1. 0x30 查询状态 → 0xA0 状态上报
     - 状态字节结构: bit0=相机连接, bit1=拍照中, bit2=录像中, bit3=预览中
  2. 0x31 查询参数 → 0xA1 参数上报
     - 参数结构体(18字节): 曝光模式+曝光值+增益+白平衡模式+RGB+分辨率
  3. 0x32 查询分辨率列表 → 0xA2 分辨率列表上报
     - 格式: 数量(1字节) + [宽(2字节)高(2字节)]...
  4. 定时状态广播: 每1秒向所有客户端广播0xA0状态上报

- **新增方法**:
  - `_handle_query_status()`: 处理状态查询
  - `_handle_query_params()`: 处理参数查询
  - `_handle_query_resolutions()`: 处理分辨率列表查询
  - `_build_status_byte()`: 构建状态字节
  - `_build_params_data()`: 构建参数数据
  - `_get_supported_resolutions()`: 获取支持的分辨率列表
  - `set_capturing()`: 设置拍照状态
  - `set_recording()`: 设置录像状态
  - `set_previewing()`: 设置预览状态
  - `set_camera()`: 绑定相机控制器

- **验收标准达成**:
  - [x] 状态查询响应正确
  - [x] 参数查询响应正确
  - [x] 分辨率列表查询正确
  - [x] 定时状态广播（1秒）

### 任务 3.4: 性能优化
- **完成时间**: 2026-01-21
- **产物**:
  - `/mnt/d/desktop/Basler/client/src/utils/performance.py` - 性能优化工具模块（新建）
  - 更新 `/mnt/d/desktop/Basler/client/src/image_acquisition.py` - 预览流优化
  - 更新 `/mnt/d/desktop/Basler/client/src/tcp_server.py` - TCP传输优化
  - 更新 `/mnt/d/desktop/Basler/client/src/image_processor.py` - 图像处理优化

- **实现要点**:

  **1. 图像处理优化（numpy vectorization）**:
  - `fast_normalize()`: 快速图像归一化
  - `fast_denormalize()`: 快速图像反归一化
  - `fast_gamma_correction()`: 使用查找表的伽马校正
  - `fast_histogram_equalization()`: 向量化直方图均衡化
  - `fast_threshold()`: 向量化二值化
  - `fast_blend()`: 向量化图像混合
  - `fast_crop()`: 零拷贝图像裁剪
  - `fast_flip()`: 零拷贝图像翻转
  - `fast_rotate_90()`: 快速90度旋转
  - `PreallocatedBuffer`: 预分配缓冲区管理器

  **2. TCP传输优化**:
  - TCP_NODELAY: 禁用Nagle算法，减少小数据包延迟
  - SO_SNDBUF/SO_RCVBUF: 调整发送/接收缓冲区大小（64KB）
  - READ_CHUNK_SIZE: 优化读取块大小（8KB）
  - 发送队列管理: 支持批量发送

  **3. 预览流优化**:
  - GrabStrategy_LatestImageOnly: 避免帧堆积
  - 跳帧策略: 网络拥塞时自动跳过帧
  - 动态JPEG质量调整: 根据带宽自动调整（30-90）
  - 性能监控: 实时统计帧率、编码时间、发送时间

  **4. 内存管理优化**:
  - `ImageBufferPool`: 图像缓冲池，减少内存分配
  - `CongestionDetector`: 网络拥塞检测器
  - `PerformanceMonitor`: 性能监控器
  - 预分配缓冲区: 双缓冲机制

- **新增类/函数**:
  - `ImageBufferPool`: 图像缓冲池
  - `CongestionDetector`: 拥塞检测器
  - `CongestionState`: 拥塞状态数据类
  - `PerformanceMonitor`: 性能监控器
  - `PerformanceMetrics`: 性能指标数据类
  - `fast_bgr_to_rgb()`: 快速颜色空间转换
  - `fast_resize_nearest()`: 快速最近邻缩放
  - `apply_brightness_contrast()`: 向量化亮度对比度调整

- **PreviewAcquisition新增方法**:
  - `_resize_image_optimized()`: 优化的图像缩放
  - `update_congestion_state()`: 更新拥塞状态
  - `set_performance_config()`: 设置性能配置
  - `get_preview_info()`: 扩展返回性能统计

- **验收标准达成**:
  - [x] 图像处理优化（numpy vectorization）
  - [x] TCP传输优化（缓冲区调整）
  - [x] 预览流优化（跳帧策略）
  - [x] 内存管理优化

## 技术笔记

### 协议版本
- 当前版本: 0x20 (v2.0)
- 主版本号: 2 (高4位)
- 次版本号: 0 (低4位)

### 命令码分类
- 控制命令 (0x10-0x3F): 上位机→客户端
- 应答命令 (0x90-0xCF): 客户端→上位机

### 关键类
- `TCPServer`: 异步TCP服务器主类
- `ProtocolParser`: 协议帧解析器（处理粘包/拆包）
- `ProtocolBuilder`: 协议帧构建器
- `ErrorCode`: 错误码枚举
- `ImageBufferPool`: 图像缓冲池
- `CongestionDetector`: 网络拥塞检测器
- `PerformanceMonitor`: 性能监控器

### 状态上报协议
- 0x30 → 0xA0: 状态字节(1字节)
- 0x31 → 0xA1: 参数结构体(18字节)
- 0x32 → 0xA2: 分辨率列表(1+N*4字节)

### 性能优化参数
- TCP缓冲区: 64KB (发送/接收)
- 读取块大小: 8KB
- 缓冲池大小: 10个缓冲区
- JPEG质量范围: 30-90（动态调整）
- 拥塞检测延迟阈值: 100ms
