# screen-mirroring-capture

<p align="center">
  <strong>通过模拟投屏设备捕获直播流 URL</strong>
</p>

<p align="center">
  <a href="https://github.com/Muchen0x/screen-mirroring-capture/blob/main/LICENSE">
    <img src="https://img.shields.io/badge/license-GPL--3.0-blue.svg" alt="License: GPL-3.0">
  </a>
  <a href="https://www.python.org/downloads/">
    <img src="https://img.shields.io/badge/python-3.10+-blue.svg" alt="Python 3.10+">
  </a>
  <a href="https://github.com/Muchen0x/screen-mirroring-capture/releases">
    <img src="https://img.shields.io/github/v/release/Muchen0x/screen-mirroring-capture.svg" alt="Release">
  </a>
</p>

---

## 简介

screen-mirroring-capture 是一个基于 [wechat-finder-dlna](https://github.com/gtoxlili/wechat-finder-dlna) 项目开发的工具，用于捕获直播流 URL。它在本地网络中模拟一个投屏设备（如电视），当您将直播投屏到该设备时，工具会捕获真实的 m3u8 流地址。

### 主要特性

- **多协议支持**：同时支持 DLNA/UPnP、AirPlay 和 Google Cast 三种投屏协议
- **图形界面**：提供直观的 tkinter GUI，方便操作
- **命令行模式**：支持通过命令行参数进行配置和使用
- **流信息检测**：自动检测流的编码格式、分辨率、帧率等信息
- **录制功能**：支持使用 FFmpeg 进行录制
- **历史记录**：保存捕获历史，方便回溯
- **播放器集成**：支持 VLC、mpv、ffplay 等播放器直接播放
- **跨平台**：支持 Windows、macOS 和 Linux（`run.bat` 仅限 Windows）

---

## 环境要求

- **Python**：3.10 或更高版本
- **FFmpeg**（可选）：用于录制功能，需要安装并添加到 PATH
- **操作系统**：Windows、macOS 或 Linux

---

## 安装

### 方式一：下载可执行文件（推荐）

从 [Releases](https://github.com/Muchen0x/screen-mirroring-capture/releases) 页面下载最新版本：

| 版本 | 说明 |
|------|------|
| **exe 安装包** | 双击运行，开箱即用，无需安装 Python |
| **OneDir 包** | 解压即用，体积较小 |

### 方式二：使用 pip 安装

```bash
# 克隆仓库
git clone https://github.com/Muchen0x/screen-mirroring-capture.git
cd screen-mirroring-capture

# 创建虚拟环境（推荐）
python -m venv venv
# Windows
venv\Scripts\activate
# macOS/Linux
source venv/bin/activate

# 安装
pip install -e .
```

### 方式三：Windows 快速启动

双击运行 `run.bat`，脚本会自动创建虚拟环境、安装依赖并启动 GUI。

### 方式四：直接安装依赖

```bash
pip install ifaddr>=0.2.0 zeroconf>=0.131.0 protobuf>=4.25.0 cryptography>=41.0.0 pycryptodome>=3.23.0
```

---

## 使用方法

### 图形界面模式（推荐）

```bash
# 前置条件：激活虚拟环境
# Windows
venv\Scripts\activate
# macOS/Linux
source venv/bin/activate

# 启动 GUI（需先执行 pip install -e .）
screen-mirroring-capture-gui

# 或（无需 pip install）
python -m screen_mirroring_capture --gui
```

**GUI 功能说明：**

1. **设备设置**：
   - 设备名称：设置投屏设备显示名称（默认：screen-mirroring-capture）
   - 网络：选择绑定的网络适配器
   - 协议：启用/禁用 DLNA、AirPlay、Cast 协议，可自定义端口

2. **捕获**：
   - 点击「开始」启动捕获服务
   - 在投屏应用中选择投屏到设备
   - 捕获成功后显示 URL 和流信息

3. **播放器**：
   - 点击「播放器」按钮配置本地播放器
   - 捕获 URL 后可直接在播放器中打开

4. **录制**：
   - 捕获 URL 后点击「录制」按钮
   - 配置保存目录、文件名、格式和时长
   - 支持实时显示录制进度

5. **历史**：
   - 查看所有捕获历史记录
   - 支持右键菜单操作：复制 URL、修改备注、录制、播放、删除

### 命令行模式

```bash
# 使用所有协议
screen-mirroring-capture

# 仅使用 DLNA
screen-mirroring-capture --protocol dlna

# 使用 AirPlay 和 Cast
screen-mirroring-capture --protocol airplay cast

# 自定义设备名称
screen-mirroring-capture --name "我的电视"

# 捕获并录制
screen-mirroring-capture --record live.mp4

# 设置录制时长（1小时）
screen-mirroring-capture --record live.mp4 --duration 01:00:00

# 管道到 VLC 播放
screen-mirroring-capture | xargs vlc

# 启用调试日志
screen-mirroring-capture -v
```

**命令行参数：**

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--name` | 投屏设备显示名称 | MAGI |
| `--port` | DLNA HTTP 基础端口 | 9090 |
| `--protocol` | 启用的协议（可多选） | 所有协议 |
| `--record` | 录制输出文件路径 | - |
| `--duration` | 录制时长（HH:MM:SS 格式） | - |
| `-v, --verbose` | 启用调试日志 | 关闭 |
| `--gui` | 启动图形界面 | - |

---

## 使用场景

### 捕获投屏直播流

1. 启动 screen-mirroring-capture
2. 在手机或电脑上打开支持投屏的应用
3. 点击应用中的「投屏」或「分享到屏幕」按钮
4. 在设备列表中选择 screen-mirroring-capture 设备
5. 工具会自动捕获直播流 URL
6. 可将 URL 复制到播放器中播放，或使用录制功能保存

### 配置播放器

1. 在 GUI 中点击「编辑」按钮
2. 点击「添加」选择播放器可执行文件
3. 支持的播放器：
   - VLC media player
   - mpv
   - ffplay (FFmpeg)
4. 捕获 URL 后，点击对应的播放器按钮即可打开

---

## 项目结构

```
screen-mirroring-capture/
├── assets/
│   └── icon.ico             # Application icon
├── screen_mirroring_capture/
│   ├── __init__.py         # 核心捕获逻辑
│   ├── __main__.py         # CLI 入口
│   ├── gui.py              # tkinter GUI
│   ├── airplay.py          # AirPlay 接收器
│   ├── cast.py             # Google Cast 接收器
│   ├── ssdp.py             # SSDP 广播
│   ├── upnp.py             # UPnP/DLNA 处理
│   ├── audio_capture.py    # 音频捕获
│   ├── descriptors.py      # XML 描述符
│   ├── net.py              # 网络工具
│   ├── pairing.py          # HAP 配对
│   └── generated/          # Protobuf 生成文件
├── pyproject.toml          # 项目配置
├── LICENSE                 # GPL-3.0 许可证
├── run.bat                 # Windows 快速启动
└── README.md               # 项目说明
```

---

## 技术原理

screen-mirroring-capture 通过以下方式工作：

1. **设备模拟**：在本地网络中模拟一个支持投屏的设备（如智能电视）
2. **服务发现**：
   - DLNA：使用 SSDP (Simple Service Discovery Protocol) 广播设备信息
   - AirPlay：使用 mDNS/Bonjour 广播 `_airplay._tcp.local.` 服务
   - Google Cast：使用 mDNS 广播 `_googlecast._tcp.local.` 服务
3. **协议实现**：
   - DLNA：实现 UPnP 设备描述和 AVTransport 服务
   - AirPlay：实现 AirPlay 2 配对握手（HAP）和 FairPlay 认证
   - Google Cast：实现 Cast V2 协议（protobuf over TLS）
4. **URL 捕获**：当发送端投屏时，解析协议消息获取媒体 URL

---

## 常见问题

### 为什么捕获不到 URL？

- 确保设备和手机在同一局域网
- 检查防火墙是否阻止了端口通信
- 尝试禁用其他协议，仅使用特定协议
- 启用调试日志查看详细信息：`screen-mirroring-capture -v`

### 如何录制直播？

1. 捕获 URL 后，在 GUI 中点击「录制」
2. 或使用命令行：`screen-mirroring-capture --record output.mp4`
3. 需要安装 FFmpeg 并添加到 PATH

### 支持哪些平台？

- Windows 10/11
- macOS 10.15+
- Linux (Ubuntu 18.04+, Debian 10+, 等)

### AirPlay 投屏后没有声音？

AirPlay 音频捕获需要手动启用：
1. 在 GUI 中勾选「录制 AirPlay 音频」
2. 选择音频保存目录
3. 投屏时音频会自动录制为 AAC 格式

---

## 致谢

本项目基于 [gtoxlili/wechat-finder-dlna](https://github.com/gtoxlili/wechat-finder-dlna) 项目开发，感谢原作者的贡献。

---

## 许可证

本项目采用 [GNU General Public License v3.0](LICENSE) 许可证。

---

## 贡献

欢迎提交 Issue 和 Pull Request！

1. Fork 本仓库
2. 创建特性分支：`git checkout -b feature/your-feature`
3. 提交更改：`git commit -m 'Add some feature'`
4. 推送到分支：`git push origin feature/your-feature`
5. 提交 Pull Request

---

## 免责声明

本工具仅供学习和研究使用。使用本工具捕获的内容请遵守相关法律法规和平台服务条款。作者不对因使用本工具产生的任何问题负责。