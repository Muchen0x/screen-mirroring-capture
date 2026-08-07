# ScreenMirroringCapture 命令大全

## CLI 命令

| 命令 | 作用 |
|---|---|
| `python -m screen_mirroring_capture` | 命令行模式，启动所有协议嗅探投屏 URL，打印到 stdout |
| `python -m screen_mirroring_capture --gui` | 启动 tkinter 图形界面 |
| `screen-mirroring-capture` | pip 安装后的 CLI 入口，同上 |
| `screen-mirroring-capture-gui` | pip 安装后的 GUI 入口，直接启动图形界面 |

## CLI 参数（`screen-mirroring-capture` / `python -m screen_mirroring_capture`）

| 参数 | 作用 |
|---|---|
| `--name NAME` | 设备名称（默认 MAGI） |
| `--port PORT` | DLNA 基础 HTTP 端口（默认 9090） |
| `--protocol PROTO [PROTO ...]` | 启用指定协议，如 `--protocol dlna airplay cast`（默认全开） |
| `--record FILE` | 捕获后用 ffmpeg 自动录制到文件 |
| `--duration HH:MM:SS` | 录制时长（ffmpeg 格式），如 `01:00:00` |
| `-v` / `--verbose` | 启用调试日志 |
| `--gui` | 启动图形界面 |

## 安装命令

| 命令 | 作用 |
|---|---|
| `pip install -e .` | 以可编辑模式安装项目到当前虚拟环境 |
| `pip install .` | 以正常模式安装项目 |
| `pip install screen-mirroring-capture` | 从 PyPI 安装（如已发布） |

## 构建命令（PyInstaller）

以下命令在项目根目录的 venv 中执行（`venv\Scripts\python.exe`），需先 `pip install -e .` 并安装 pyinstaller。

| 命令 | 作用 | 输出 |
|---|---|---|
| `venv\Scripts\python.exe -m PyInstaller --onefile --noconsole --name ScreenMirroringCapture --icon=assets\icon.ico --add-data "assets\icon.ico;assets" --version-file version.txt --hidden-import screen_mirroring_capture.gui run.py` | 构建单文件 exe | `dist\ScreenMirroringCapture.exe`，约 18.5MB |
| 同上，去掉 `--onefile` | 构建目录 exe | `dist\ScreenMirroringCapture\`（含 `_internal\`），约 4.1MB + 依赖 |
| `Compress-Archive -Path dist\ScreenMirroringCapture -DestinationPath dist\ScreenMirroringCapture-<版本>-OneDir.zip -Force` | 打包 OneDir 目录为 zip | `dist\ScreenMirroringCapture-<版本>-OneDir.zip` |

## 辅助脚本

| 命令 | 作用 |
|---|---|
| `run.bat` | 自动创建 venv → `pip install -e .` → 启动 GUI |
| `python run.py` | PyInstaller 入口包装，启动 GUI（直接运行也可） |
