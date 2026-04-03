# Basler-acA5472-5gc

Basler 相机控制项目，包含两部分：

- `client/`：相机端服务程序，负责连接 Basler 相机、抓图、录像、预览和 TCP 控制服务
- `gui/`：上位机界面程序，负责连接相机端服务并下发控制指令

## 目录结构

- `client/`
  相机端程序源码、配置和离线安装说明
- `gui/`
  上位机源码和配置
- `Record/`
  历史录像相关目录

## 运行环境

推荐在 Windows 64 位环境运行。

相机端必备：

- Python 3.13
- Basler `pylon Camera Software Suite`
- `pylon SDK`
- `pylon Viewer`
- Python 依赖：`loguru`、`numpy`、`opencv-python`、`Pillow`、`pypylon`

GUI 必备：

- Python 3.13
- Python 依赖：`loguru`、`Pillow`、`numpy`

相机端离线安装参考：
[INSTALL.txt](/mnt/c/desktop/work-zzmt/Basler_camera/Basler-acA5472-5gc/client/win64_install/INSTALL.txt)

## 安装依赖

### 相机端

```bat
cd /d C:\desktop\work-zzmt\Basler_camera\Basler-acA5472-5gc\client
pip install -r requirements.txt
```

说明：

- `requirements.txt` 中未直接写入 `pypylon`，因为它依赖本机已安装的 Basler `pylon SDK`
- 如需离线安装，可按 `client/win64_install/INSTALL.txt` 里的流程安装

### GUI

```bat
cd /d C:\desktop\work-zzmt\Basler_camera\Basler-acA5472-5gc\gui
pip install -r requirements.txt
```

## 启动命令

### 1. 启动相机端服务

```bat
cd /d C:\desktop\work-zzmt\Basler_camera\Basler-acA5472-5gc\client\src
python main.py
```

默认配置文件：
[config.json](/mnt/c/desktop/work-zzmt/Basler_camera/Basler-acA5472-5gc/client/config/config.json)

默认监听：

- `host = 0.0.0.0`
- `port = 8899`

### 2. 启动 GUI

```bat
cd /d C:\desktop\work-zzmt\Basler_camera\Basler-acA5472-5gc\gui\src
python main.py
```

默认 GUI 连接配置：
[settings.json](/mnt/c/desktop/work-zzmt/Basler_camera/Basler-acA5472-5gc/gui/config/settings.json)

默认连接目标：

- `host = 127.0.0.1`
- `port = 8899`

如果 GUI 和相机端不在同一台机器上，需要把 GUI 配置里的 `host` 改成相机端所在主机 IP。

## 启动顺序

1. 先确认 Basler 相机已连接
2. 关闭 `pylon Viewer`
3. 启动相机端服务
4. 启动 GUI
5. 在 GUI 中连接到相机端服务

## 当前闪光灯控制语义

当前项目已切回相机内部定时器触发方案：

- 输出链路：`Line2 + Timer1Active`
- 闪光灯设置命令：`SET_FLASH(0x27)`
- GUI 输入项含义：`追加延时(ms)`

实际总延时计算方式：

```text
总延时 = 60100ms + 用户输入的追加延时
```

例如：

- GUI 输入 `0`
  实际总延时为 `60100ms`
- GUI 输入 `100`
  实际总延时为 `60200ms`

当前实现还包含：

- 固定定时器脉宽：`1000us`
- 录像和预览期间会自动关闭 `Line2` 闪光输出
- 录像和预览结束后会恢复用户最近一次设置的闪光参数

## 常见问题

### 相机端启动后提示相机连接失败

优先检查：

- 相机是否已经接好
- `pylon Viewer` 是否已关闭
- 本机是否正确安装了 `pylon SDK`
- `pypylon` 是否可用

### GUI 连不上相机端

优先检查：

- 相机端服务是否已经启动
- 端口是否为 `8899`
- GUI 配置中的 `host` 是否正确
- Windows 防火墙是否拦截了 Python 进程或对应端口

### 修改闪光延时后效果不对

先确认你输入的是“追加延时”，不是总延时。  
程序内部会自动在你输入值前加上固定基准 `60100ms`。

## 相关入口文件

- 相机端入口：
  [main.py](/mnt/c/desktop/work-zzmt/Basler_camera/Basler-acA5472-5gc/client/src/main.py)
- GUI 入口：
  [main.py](/mnt/c/desktop/work-zzmt/Basler_camera/Basler-acA5472-5gc/gui/src/main.py)
