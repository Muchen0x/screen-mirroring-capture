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
- **图形界面**：提供直观的 tkinter GUI，方便操作；窗口缩放时控件自动换行，不会挤掉或遮挡
- **命令行模式**：支持通过命令行参数进行配置和使用
- **单实例保护**：重复启动不会多开窗口，而是把已有窗口唤醒到前台，避免争用同一设备名与端口
- **流信息检测**：自动检测流的编码格式、分辨率、帧率等信息
- **播放器集成**：支持 VLC、mpv、ffplay 等播放器直接播放，可配置多个播放器并为每个播放器单独指定播放协议
- **播放列表**：捕获结果可加入队列，支持去重、备注，以及「排队追加 / 立即切换」两种播放方式
- **自动播放**：捕获即交给指定播放器，带同 URL 防抖、杀旧进程等选项
- **录制功能**：支持使用 FFmpeg 进行录制，可自定义流地址、格式与时长
- **历史记录**：保存捕获历史，方便回溯
- **跨平台**：支持 Windows、macOS 和 Linux（`run.bat`、`launch.vbs` 与单实例唤起仅限 Windows）

---

## 环境要求

- **Python**：3.10 或更高版本
- **FFmpeg**（可选）：用于录制、流信息解析（ffprobe）与流时长探测；缺失时捕获功能本身仍可用
- **操作系统**：Windows、macOS 或 Linux

---

## 安装

### 方式一：下载可执行文件（推荐）

从 [Releases](https://github.com/Muchen0x/screen-mirroring-capture/releases) 页面下载最新版本（当前为 **v1.7.0**）：

| 版本 | 说明 |
|------|------|
| **单文件 exe** | 一个可执行文件，双击运行，开箱即用，无需安装 Python。每次启动都要先把自身解压到临时目录，启动比 OneDir 版慢几秒 |
| **OneDir 包** | 解压到任意目录后运行其中的 `ScreenMirroringCapture.exe`，启动更快；依赖都在 `_internal` 子目录里，移动时请整个文件夹一起移 |

> 两个包体积接近（均约 20 MB）。想拷到 U 盘随手用选单文件版，想长期放在固定目录、频繁启动选 OneDir 包。
> 程序会在 exe 同目录生成 `config/`（配置与历史）和 `log/`（运行日志），请确保该目录可写。

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

不想看到控制台窗口时，双击 `launch.vbs`——它会以隐藏窗口的方式拉起 `run.bat`，只显示图形界面。

### 方式四：直接安装依赖

```bash
pip install "ifaddr>=0.2.0" "zeroconf>=0.131.0" "protobuf>=4.25.0" "cryptography>=41.0.0" "pycryptodome>=3.23.0" "pywin32>=306"
```

> `pywin32` 仅 Windows 需要（mpv 命名管道播放）。缺少它时 mpv 会静默退化成「新开一个进程」播放，无法复用已打开的 mpv 窗口。

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

> **单实例**：GUI 同一时间只允许一个窗口。已有实例在运行时，再次启动不会打开第二个窗口，而是把已有窗口从最小化恢复并置顶，本次启动随即退出（退出码 `3`，`run.bat` 已针对它做处理，不会留下一个等待按键的控制台）。这样可以避免两个实例争用同一个设备名和同一组端口。

**GUI 功能说明：**

1. **设备设置**：
   - 设备名称：设置投屏设备显示名称（默认：screen-mirroring-capture）
   - 网络：选择绑定的网络适配器
   - 协议：勾选启用 DLNA / AirPlay / Cast，并为每个协议单独指定端口
   - 「恢复默认」把协议开关与端口重置为默认值

2. **捕获**：
   - 点击「▶ 开始」启动捕获服务，按钮随即变为「⏹ 停止」；停止过程中显示「⏳ 停止中」，此时仍可再次点击
   - 「持续捕获」：连续捕获多条 URL，捕获到后不自动停止
   - 在投屏应用中选择投屏到设备，成功后显示 URL 与流信息，「📋 复制」一键复制
   - 备注：给当前 URL 打标签，随历史记录与播放列表一起保存

3. **播放器**：
   - 点击「编辑」配置本地播放器（名称 + 可执行文件路径），可添加多个，也可脚本形式填写播放命令
   - 每个播放器在界面上对应一个按钮，捕获到 URL 后点击即可播放
   - 每个播放器可分别设置「自动播放协议」与「手动播放协议」：`auto`（按 URL 扩展名自动选择：`.m3u8`/`.flv` 直传，其余生成 m3u 播放列表）、`direct`（原始 URL 直接传给播放器并重启进程）、`playlist`（始终生成 m3u 播放列表文件再交给播放器）、`ipc`（仅 mpv：通过 Windows 命名管道向已运行的 mpv 发送 `loadfile`，不新开进程）

4. **播放列表**：
   - 「加入播放列表」把当前 URL 收进队列，也可在捕获历史中选中记录后加入
   - 支持「自动去重」「去重」「添加当前URL」「清空」；右键菜单可复制 URL、修改备注、录制、播放、删除；双击复制 URL
   - 底部按钮切换播放方式：**排队**（`append-play`，播完当前内容再接新 URL）或**立即切换**（`replace`，立刻替换当前内容），设置会写回配置文件，鼠标悬停可看到说明

5. **自动播放**：
   - 勾选「自动播放」并选择播放器，之后每次捕获到 URL 都会自动交给该播放器播放；勾选动作本身不会立刻播放当前已捕获的 URL
   - 「持续捕获」模式下改由发送端触发播放时自动接着播，同一 URL 3 秒内不会重复起播
   - 「防抖(秒)」：手动点击播放器按钮的最小间隔，防止连点重复起播（默认 2 秒，可设 0.5–10）
   - 「杀旧进程」：播放前结束遗留的旧播放器进程；「全局直连」：所有播放都强制走直连模式（忽略播放器各自的协议设置），可作为 IPC 异常时的兜底

6. **录制**：
   - 捕获 URL 后点击「录制」按钮
   - 配置保存目录、文件名、格式和时长；对话框顶部可修改流地址，打开时会自动探测流时长并回填（不覆盖手动选择）
   - 支持实时显示录制进度与百分比
   - 勾选「录制 AirPlay 音频」可把音频另存为 AAC

7. **历史**：
   - 查看所有捕获历史记录
   - 支持右键菜单操作：复制 URL、修改备注、录制、播放、加入播放列表、删除

8. **日志**：
   - 底部面板可在「当前日志 / 历史日志」之间切换，「启用调试日志」输出更详细的信息，另有「清除当前日志」
   - 拖动分隔条或拉高窗口可放大日志区

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

1. 在 GUI 中点击「编辑」按钮，打开「播放器管理」
2. 点击「添加」，填写播放器名称并选择可执行文件路径（也可直接填整条播放命令），可同时指定自动/手动播放协议
3. 支持的播放器（同类的其他播放器一般也能用）：
   - VLC media player
   - mpv
   - ffplay (FFmpeg)
4. 「上移 / 下移」调整顺序，主窗口上的播放器按钮排列会跟着变化；「删除」移除选中项
5. 捕获 URL 后，点击对应的播放器按钮即可打开

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
│   ├── single_instance.py  # GUI 单实例守卫
│   └── generated/          # Protobuf 生成文件
├── config/                 # 运行时生成：config.json / history.json
├── log/                    # 运行时生成：运行日志与 faulthandler 转储
├── pyproject.toml          # 项目配置（依赖、入口点）
├── version.txt             # Windows 文件版本信息
├── run.py                  # PyInstaller 打包入口
├── run.bat                 # Windows 快速启动（自动建 venv）
├── launch.vbs              # Windows 静默启动（无控制台窗口）
├── CHANGELOG.md            # 版本更新日志
├── COMMANDS.md             # CLI / 构建命令速查
├── LICENSE                 # GPL-3.0 许可证
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

## 更新日志

完整变更记录见 [CHANGELOG.md](CHANGELOG.md)，按版本给出「修改前 / 修改后」对照。

- **v1.7.0**（2026-11-16）：新增单实例守卫，重复启动会唤醒已有窗口而不再多开；修复连续捕获恒为空、端口非法即失效、AirPlay 音频未接通等 P0 问题；SSDP 通告降频约 150 倍、UPnP 描述 XML 转义、播放器进程回收等稳定性改进；界面改为自动换行，窗口缩放耗时从 371ms 降到 82ms；捕获控制行重排，开始/停止合并为一个按钮；下拉框不再被滚轮改值、点击后不留虚线焦点框；所有子窗口统一应用图标。
- **v1.6.0**（2026-08-29）：自动播放行为重构；UI 线程改为队列派发，修复全线程冻结；修复 ffplay 统计流导致的日志卡死；录制支持任意 URL 并改进 ffmpeg/ffprobe 探测。

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

> 「重复启动唤醒已有窗口」依赖 Windows 命名互斥体与窗口消息，仅在 Windows 生效；其他平台退化为临时文件锁，只保证不开第二个实例。

### 双击后没反应，或者又开了一个窗口？

v1.7.0 起 GUI 为单实例：

- 已有实例在运行时再次双击，会把已运行的窗口从最小化恢复并置顶，本次启动以退出码 `3` 结束（`run.bat` 已识别该退出码，不会停在 `pause`）。
- 如果双击后毫无反应，检查窗口是否被最小化、被其他窗口遮挡或落在另一个虚拟桌面上；也可以先关掉所有 `ScreenMirroringCapture.exe` 进程再启动。
- 两个实例会抢同一设备名和同一组端口，请避免同时运行——这也是单实例存在的原因。

### 配置和历史记录保存在哪里？

都放在程序同目录下：

- `config/config.json`：设备名、协议开关与端口、播放器列表、自动播放/播放列表等设置
- `config/history.json`：捕获历史
- `log/`：运行日志；异常时还会写入 faulthandler 转储

想恢复出厂设置，退出程序后删掉 `config/` 目录再启动即可（会重新生成默认配置）。单文件 exe 版中，`config/` 位于 exe 所在目录，而非临时解压目录。

### 提示「未找到 ffmpeg/ffprobe」怎么办？

程序按以下顺序查找：

1. `config/config.json` 中的 `ffmpeg_dir` 目录
2. 已配置播放器所在目录
3. 系统 `PATH`
4. 常见安装位置（如 `%LOCALAPPDATA%\ffmpeg\bin`）

最省事的做法是把 FFmpeg 解压到任意目录，然后在 `config.json` 里加一行 `"ffmpeg_dir": "D:\\ffmpeg\\bin"`。缺 ffprobe 只影响流信息解析与时长探测，不影响捕获本身。

### AirPlay 投屏后没有声音？

AirPlay 音频捕获需要手动启用：
1. 在 GUI 中勾选「录制 AirPlay 音频」
2. 在后面的输入框填写音频输出**文件路径**（如 `D:\capture\airplay.aac`，不是目录），或点「浏览」选择
3. 投屏时音频会自动录制为该 AAC 文件

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