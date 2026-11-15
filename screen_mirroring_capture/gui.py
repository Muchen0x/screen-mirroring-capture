"""tkinter GUI for screen-mirroring-capture.

Launch with::

    python -m screen_mirroring_capture --gui
    screen-mirroring-capture-gui
"""

from __future__ import annotations

import faulthandler
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Callable

from . import PROTOCOLS, capture
from .net import get_lan_ip, list_adapters

log = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────

if getattr(sys, "frozen", False):
    if "_MEI" in str(sys.executable):
        BASE_DIR = Path(sys.argv[0]).resolve().parent
    else:
        BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
LOG_DIR = BASE_DIR / "log"
CONFIG_FILE = CONFIG_DIR / "config.json"
HISTORY_FILE = CONFIG_DIR / "history.json"

# Format mapping: URL extension -> (default save extension, format name)
_EXT_MAP: dict[str, tuple[str, str]] = {
    ".m3u8": (".mp4", "MP4"),
    ".flv": (".flv", "FLV"),
    ".mpd": (".mp4", "MP4"),
    ".mp4": (".mp4", "MP4"),
    ".mkv": (".mkv", "MKV"),
    ".webm": (".webm", "WebM"),
    ".ts": (".ts", "TS"),
    ".avi": (".avi", "AVI"),
    ".mov": (".mov", "MOV"),
    ".wmv": (".wmv", "WMV"),
    ".mp3": (".mp3", "MP3"),
    ".aac": (".aac", "AAC"),
    ".wav": (".wav", "WAV"),
    ".ogg": (".ogg", "OGG"),
    ".flac": (".flac", "FLAC"),
}

# All available format options for the dropdown
_FORMAT_OPTIONS = [
    (".mp4", "MP4"),
    (".mkv", "MKV"),
    (".ts", "TS"),
    (".flv", "FLV"),
    (".webm", "WebM"),
    (".avi", "AVI"),
    (".mov", "MOV"),
    (".wmv", "WMV"),
    (".aac", "AAC"),
    (".mp3", "MP3"),
    (".wav", "WAV"),
    (".ogg", "OGG"),
    (".flac", "FLAC"),
]

MAX_HISTORY_RECORDS = 1000
MAX_HISTORY_LOG_SIZE = 20 * 1024 * 1024  # 20 MB

# history.json is read-modify-written from both the Tk main thread and from
# ffprobe worker threads, so every mutation must hold this lock.
_HISTORY_LOCK = threading.RLock()

# Default port per protocol — single source of truth for the UI and validation.
_DEFAULT_PORTS: dict[str, int] = {"dlna": 9090, "airplay": 9091, "cast": 8009}

# Playlist metadata fields filled in by an asynchronous ffprobe pass.
_META_FIELDS = ("duration", "resolution", "video_codec", "audio_codec", "bitrate", "fps")

# Window resizes arrive as a flood of <Configure> events (~60/s while a mouse
# button drags the edge). Re-flowing the content for every one of them costs
# more than the interval between two events, so the panel is only re-flowed
# once the drag settles.
_RESIZE_SETTLE_MS = 180

# How a URL is handed to an already-running mpv over the IPC pipe.
#   append-play: append to the queue, start only when nothing is playing
#   replace:     switch to the new URL immediately, dropping the current one
IPC_MODE_QUEUE = "append-play"
IPC_MODE_REPLACE = "replace"
IPC_MODES = (IPC_MODE_QUEUE, IPC_MODE_REPLACE)

_IPC_MODE_HINT = (
    "mpv 正在运行时的点播方式：\n\n"
    "▸ 排队（append-play）\n"
    "  新 URL 追加到 mpv 队列，当前内容播完后接着播，\n"
    "  不打断正在看的内容；空闲时自动开始播放\n\n"
    "▸ 立即切换（replace）\n"
    "  立刻用新 URL 替换当前正在播放的内容，不排队"
)


def _get_ipc_mode(config: dict) -> str:
    """Return the configured IPC loadfile mode, falling back to queueing."""
    mode = config.get("mpv_ipc_mode", IPC_MODE_QUEUE)
    return mode if mode in IPC_MODES else IPC_MODE_QUEUE


_PROTOCOL_HELP = (
    "各协议选项含义：\n\n"
    "▸ auto（自动模式）— 推荐\n"
    "  根据 URL 扩展名自动选择播放方式：\n"
    "  · .m3u8 / .flv → URL 直接传递给播放器\n"
    "  · 其他格式 → 自动生成 .m3u 播放列表\n"
    "    文件后传递给播放器\n\n"
    "▸ direct（直连模式）\n"
    "  不做任何检测或预处理，直接用原始 URL\n"
    "  重启播放器进程\n\n"
    "▸ playlist（列表模式）\n"
    "  始终生成 .m3u 播放列表文件，再将文件\n"
    "  传递给播放器。适用于 VLC、MPC-HC、\n"
    "  MPC-BE、mpv 等\n\n"
    "▸ ipc（进程通信模式）— 仅 mpv\n"
    "  不启动新进程，通过 Windows 命名管道\n"
    "  向已有 mpv 发送 loadfile 指令。是排队\n"
    "  还是立即切换，可在播放列表窗口底部的\n"
    "  「播放方式」按钮里切换"
)

def _get_player_path(entry: str | dict) -> str:
    if isinstance(entry, dict):
        return entry.get("path", "")
    return entry


def _get_player_protocol(entry: str | dict) -> str:
    if isinstance(entry, dict):
        return entry.get("protocol", "auto")
    return "auto"


def _get_player_manual_protocol(entry: str | dict) -> str:
    if isinstance(entry, dict):
        return entry.get("manual_protocol", "auto")
    return "auto"


def _parse_port(raw: object) -> int | None:
    """Parse a port entry.

    Returns:
        The port number, or ``None`` when *raw* is not an integer in 1-65535.
    """
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if 1 <= value <= 65535 else None


# ttk draws a dotted focus ring as soon as a button-like widget holds keyboard
# focus, and a plain mouse click is enough to hand it that focus.  These helpers
# keep the ring off mouse interaction without touching Tab navigation.
_BUTTON_CLASSES = frozenset({
    "TButton", "TCheckbutton", "TRadiobutton", "TMenubutton",
})


def _drop_focus(widget: tk.Misc) -> None:
    """Hand keyboard focus from *widget* back to its window."""
    try:
        if widget.focus_get() is widget:
            widget.winfo_toplevel().focus_set()
    except (tk.TclError, KeyError, AttributeError):
        pass


def _bind_enter_commit(entry: tk.Misc, commit=None) -> None:
    """Make Enter commit the value of *entry* and release its keyboard focus."""

    def handler(event: tk.Event | None = None) -> str:
        try:
            if commit is not None:
                commit()
        except Exception:
            log.exception("回车提交失败: %r", entry)
        finally:
            _drop_focus(entry)
        return "break"

    entry.bind("<Return>", handler)
    entry.bind("<KP_Enter>", handler)


# The capture card uses one button for start/stop.  Three labels keep the
# button width stable while telling the three states apart.
_RUN_BTN_TEXT = {
    "idle": "▶ 开始",
    "running": "⏹ 停止",
    "stopping": "⏳ 停止中",
}


def _log_stderr(proc: subprocess.Popen, prefix: str = "") -> None:
    _MAX_LINE = 4000

    def _reader():
        buf = b""
        while True:
            try:
                chunk = proc.stderr.read(4096)
            except Exception:
                return
            if not chunk:
                break
            buf += chunk
            *lines, buf = re.split(rb"[\r\n]+", buf)
            for line in lines:
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                if len(text) > _MAX_LINE:
                    text = text[:_MAX_LINE] + f"...(截断 {len(text)} 字符)"
                log.debug("%sstderr: %s", prefix, text)
        if buf:
            text = buf.decode("utf-8", errors="replace").strip()
            if text:
                if len(text) > _MAX_LINE:
                    text = text[:_MAX_LINE] + f"...(截断 {len(text)} 字符)"
                log.debug("%sstderr: %s", prefix, text)
    threading.Thread(target=_reader, daemon=True).start()


def _find_player_executable(path_or_cmd: str | dict) -> str | None:
    if isinstance(path_or_cmd, dict):
        path_or_cmd = path_or_cmd.get("path", "")
    if not path_or_cmd:
        log.debug("_find_player_executable: empty input")
        return None
    p = Path(path_or_cmd)
    if p.is_absolute() and p.exists():
        log.debug("_find_player_executable: found at absolute path %s", p)
        return str(p)
    found = shutil.which(path_or_cmd)
    if found:
        log.debug("_find_player_executable: found via PATH → %s", found)
        return found
    if "vlc" in path_or_cmd.lower():
        common = [
            Path("C:/Program Files/VideoLAN/VLC/vlc.exe"),
            Path("C:/Program Files (x86)/VideoLAN/VLC/vlc.exe"),
            Path.home() / "AppData/Local/Programs/VLC/vlc.exe",
        ]
        for fp in common:
            if fp.exists():
                log.debug("_find_player_executable: found VLC at fallback %s", fp)
                return str(fp)
        log.debug("_find_player_executable: VLC fallback paths all missing")
    elif "mpv" in path_or_cmd.lower():
        common = [
            Path("C:/mpv/mpv.exe"),
            Path.home() / "scoop/apps/mpv/current/mpv.exe",
        ]
        for fp in common:
            if fp.exists():
                log.debug("_find_player_executable: found mpv at fallback %s", fp)
                return str(fp)
        log.debug("_find_player_executable: mpv fallback paths all missing")
    else:
        log.debug("_find_player_executable: %s not found anywhere", path_or_cmd)
    return None


def _find_tool(name: str) -> str | None:
    """Locate an ffmpeg-family tool (ffmpeg/ffprobe/ffplay).

    Search order:
      1. config.json optional "ffmpeg_dir" directory
      2. same directory as any configured player executable
      3. PATH via shutil.which
      4. common install locations

    Returns:
        Full path to the tool executable, or None if not found.
    """
    exe_name = f"{name}.exe"
    try:
        cfg = _load_config()
        ffmpeg_dir = cfg.get("ffmpeg_dir", "")
        if ffmpeg_dir:
            fp = Path(ffmpeg_dir) / exe_name
            if fp.is_file():
                log.debug("_find_tool: %s found via config ffmpeg_dir → %s", name, fp)
                return str(fp)
            log.debug("_find_tool: config ffmpeg_dir %s has no %s", ffmpeg_dir, exe_name)
        for player in (cfg.get("players") or {}).values():
            path = player.get("path", "") if isinstance(player, dict) else player
            if not path:
                continue
            p = Path(path)
            if not (p.is_absolute() and p.parent.is_dir()):
                continue
            fp = p.parent / exe_name
            if fp.is_file():
                log.debug("_find_tool: %s found next to player %s → %s", name, p.name, fp)
                return str(fp)
    except Exception:
        log.debug("_find_tool: config lookup failed", exc_info=True)
    found = shutil.which(name)
    if found:
        log.debug("_find_tool: %s found via PATH → %s", name, found)
        return found
    common = [
        Path(os.environ.get("SystemDrive", "C:")) / "ffmpeg" / "bin",
        Path("C:/Program Files/ffmpeg/bin"),
        Path("C:/Program Files (x86)/ffmpeg/bin"),
        Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "ffmpeg" / "bin",
        Path.home() / "scoop" / "apps" / "ffmpeg" / "current" / "bin",
    ]
    for fp in common:
        cand = fp / exe_name
        if cand.is_file():
            log.debug("_find_tool: %s found at common location %s", name, cand)
            return str(cand)
    log.debug("_find_tool: %s not found anywhere", name)
    return None


def _seed_default_players() -> dict[str, str]:
    """Detect common media players on the system.

    Returns:
        Dict of display name -> executable path / command.
    """
    known = {
        "vlc": ("VLC", "VLC media player"),
        "mpv": ("mpv", "mpv media player"),
        "ffplay": ("ffplay", "ffplay (FFmpeg)"),
    }
    result = {}
    for cmd, (name, _) in known.items():
        exe = _find_player_executable(cmd)
        if exe:
            result[name] = exe
    return result


def _load_config() -> dict:
    """Load configuration from file."""
    try:
        if CONFIG_FILE.exists():
            config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            config.pop("default_player", None)
            if "players" not in config or not config["players"]:
                config["players"] = _seed_default_players()
                _save_config(config)
            return config
    except Exception:
        log.debug("Failed to load config", exc_info=True)
    config = {"players": _seed_default_players()}
    _save_config(config)
    return config


def _save_config(config: dict) -> None:
    """Save configuration to file."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        log.debug("Failed to save config", exc_info=True)


def _load_history() -> list:
    """Load capture history from file."""
    try:
        if HISTORY_FILE.exists():
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8")).get("records", [])
    except Exception:
        log.debug("Failed to load history", exc_info=True)
    return []


def _save_history(records: list) -> None:
    """Save capture history to file."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        HISTORY_FILE.write_text(
            json.dumps({"records": records}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        log.debug("Failed to save history", exc_info=True)


def _add_history_record(url: str, info: dict, remark: str = "") -> None:
    """Add a capture record to history."""
    with _HISTORY_LOCK:
        records = _load_history()
        # Derive the id from the highest existing id: using len(records) + 1
        # would collide with a surviving record after any deletion.
        next_id = max((r.get("id", 0) for r in records), default=0) + 1
        record = {
            "id": next_id,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "url": url,
            "duration": info.get("duration", "未知"),
            "video_codec": info.get("video_codec", ""),
            "resolution": info.get("resolution", ""),
            "fps": info.get("fps", ""),
            "audio_codec": info.get("audio_codec", ""),
            "bitrate": info.get("bitrate", ""),
            "recorded": False,
            "output": None,
            "remark": remark,
        }
        records.append(record)
        if len(records) > MAX_HISTORY_RECORDS:
            records = records[-MAX_HISTORY_RECORDS:]
        _save_history(records)


def _mark_history_record(url: str, output_path: str) -> None:
    """Mark the most recent matching history record as recorded."""
    with _HISTORY_LOCK:
        records = _load_history()
        for record in reversed(records):
            if record["url"] == url and not record.get("recorded"):
                record["recorded"] = True
                record["output"] = output_path
                break
        _save_history(records)


def _load_recent_logs(max_lines: int = 200) -> list[str]:
    """Load recent log lines from history.log file."""
    try:
        history_file = LOG_DIR / "history.log"
        if history_file.exists():
            lines = history_file.read_text(encoding="utf-8").splitlines()
            return lines[-max_lines:]
    except Exception:
        pass
    return []


def _merge_session_to_history(session_file: Path) -> None:
    """Append current session log to history.log and delete session file."""
    try:
        if not session_file.exists():
            return
        history_file = LOG_DIR / "history.log"
        with open(history_file, "a", encoding="utf-8") as hf:
            hf.write(session_file.read_text(encoding="utf-8"))
            hf.write("\n")
        session_file.unlink()
        if history_file.exists() and history_file.stat().st_size > MAX_HISTORY_LOG_SIZE:
            lines = history_file.read_text(encoding="utf-8").splitlines()
            mid = len(lines) // 2
            history_file.write_text("\n".join(lines[mid:]) + "\n", encoding="utf-8")
    except Exception:
        log.debug("Failed to merge session to history", exc_info=True)


def _clear_log_files(mode: str = "history", current_file: Path = None) -> None:
    """Delete log files based on mode.

    Args:
        mode: "current" to delete current session file,
              "history" to delete history.log file.
        current_file: Path to current session log file.
    """
    try:
        if mode == "current":
            if current_file and current_file.exists():
                current_file.unlink()
        elif mode == "history":
            history_file = LOG_DIR / "history.log"
            if history_file.exists():
                history_file.unlink()
    except Exception:
        log.debug("Failed to clear log files", exc_info=True)


def _cleanup_stale_files(days: int = 3) -> None:
    """Remove orphaned session logs and faulthandler dumps older than *days*."""
    try:
        cutoff = datetime.now().timestamp() - days * 86400
        for f in LOG_DIR.iterdir():
            if not f.is_file():
                continue
            name = f.name
            try:
                if name.startswith("session_") and name.endswith(".log"):
                    ts_str = name[len("session_"):-len(".log")]
                    ts = datetime.strptime(ts_str, "%Y-%m-%d_%H-%M-%S").timestamp()
                elif name.startswith("faulthandler-") and name.endswith(".txt"):
                    ts_str = name[len("faulthandler-"):-len(".txt")]
                    ts = datetime.strptime(ts_str, "%Y%m%d-%H%M%S").timestamp()
                else:
                    continue
                if ts < cutoff:
                    f.unlink()
            except Exception:
                pass
    except Exception:
        log.debug("Failed to cleanup stale files", exc_info=True)


def _detect_format(url: str) -> tuple[str, str]:
    """Detect format from URL. Returns (extension, format_name)."""
    url_lower = url.lower()
    for ext, (dext, name) in _EXT_MAP.items():
        if ext in url_lower:
            return dext, name
    return ".mp4", "MP4"


def _get_duration(url: str) -> tuple[float | None, str]:
    """Get duration of a URL using ffprobe."""
    try:
        ffprobe = _find_tool("ffprobe")
        if not ffprobe:
            return None, "未知"
        cmd = [
            ffprobe, "-v", "quiet", "-print_format", "json",
            "-show_format", "-allowed_extensions", "ALL", "-extension_picky", "0", url,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            duration = float(data["format"]["duration"])
            if duration > 0:
                return duration, _format_duration(duration)
            else:
                return None, "直播中"
    except Exception:
        pass
    return None, "未知"


def _format_duration(seconds: float | None) -> str:
    """Format duration seconds to HH:MM:SS string."""
    if seconds is None or seconds <= 0:
        return "未知"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours > 0 else f"{minutes:02d}:{secs:02d}"


def _parse_time_to_seconds(time_str: str) -> float:
    """Parse HH:MM:SS.xx time string to seconds."""
    try:
        parts = time_str.split(":")
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    except (ValueError, IndexError):
        pass
    return 0.0


def _format_size(raw: str) -> str:
    """Convert ffmpeg size string (e.g. 12345kB) to auto-scaled human-readable format."""
    m = re.match(r"(\d+)(\w+)", raw.strip())
    if not m:
        return raw
    value = float(m.group(1))
    unit = m.group(2).lower().replace("i", "")
    if unit in ("kb", "k"):
        if value >= 1024 * 1024:
            return f"{value / (1024 * 1024):.2f} GB"
        if value >= 1024:
            return f"{value / 1024:.2f} MB"
        return f"{value:.0f} KB"
    if unit in ("mb", "m"):
        if value >= 1024:
            return f"{value / 1024:.2f} GB"
        return f"{value:.2f} MB"
    if unit in ("gb", "g"):
        return f"{value:.2f} GB"
    return raw


def _get_stream_info(url: str) -> dict:
    """Get detailed stream information using ffprobe.

    Returns {} on parse failure, or {"error": msg} when ffprobe is unavailable.
    """
    try:
        ffprobe = _find_tool("ffprobe")
        if not ffprobe:
            return {"error": "未找到 ffprobe"}
        cmd = [
            ffprobe, "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", "-allowed_extensions", "ALL",
            "-extension_picky", "0", url,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode == 0:
            return json.loads(result.stdout)
    except Exception:
        pass
    return {}


def _parse_stream_info(data: dict) -> dict:
    """Parse stream information from ffprobe output."""
    info = {}
    if "format" in data:
        duration = float(data["format"].get("duration", 0))
        info["duration"] = _format_duration(duration) if duration > 0 else "直播中"
        bitrate = int(data["format"].get("bit_rate", 0))
        if bitrate > 0:
            info["bitrate"] = f"{bitrate // 1000} kbps"
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and "video_codec" not in info:
            codec = stream.get("codec_name", "").upper()
            profile = stream.get("profile", "")
            info["video_codec"] = f"{codec} ({profile})" if profile else codec
            w, h = stream.get("width", 0), stream.get("height", 0)
            if w and h:
                info["resolution"] = f"{w}x{h}"
            try:
                num, den = stream.get("r_frame_rate", "0/1").split("/")
                fps = float(num) / float(den)
                if fps > 0:
                    info["fps"] = f"{fps:.2f} fps"
            except (ValueError, ZeroDivisionError):
                pass
        elif stream.get("codec_type") == "audio" and "audio_codec" not in info:
            info["audio_codec"] = stream.get("codec_name", "").upper()
            sr = stream.get("sample_rate", "")
            if sr:
                info["sample_rate"] = f"{sr} Hz"
    return info


def _make_playlist_item(url: str, info: dict | None = None, remark: str = "") -> dict:
    """Build a playlist item from already-known metadata.

    This performs *no* I/O: it must stay cheap because it is called from Tk
    callbacks.  Missing metadata is filled in later by an async ffprobe pass.
    """
    info = info or {}
    return {
        "url": url,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "duration": info.get("duration", ""),
        "resolution": info.get("resolution", ""),
        "video_codec": info.get("video_codec", ""),
        "audio_codec": info.get("audio_codec", ""),
        "bitrate": info.get("bitrate", ""),
        "fps": info.get("fps", ""),
        "remark": remark,
    }


def _format_stream_info(info: dict) -> tuple[str, str]:
    """Format stream info into two display lines."""
    parts1 = []
    if "duration" in info:
        parts1.append(f"时长: {info['duration']}")
    if "video_codec" in info:
        parts1.append(f"编码: {info['video_codec']}")
    if "resolution" in info:
        parts1.append(f"分辨率: {info['resolution']}")
    parts2 = []
    if "fps" in info:
        parts2.append(f"帧率: {info['fps']}")
    if "audio_codec" in info:
        audio = info["audio_codec"]
        if "sample_rate" in info:
            audio += f" {info['sample_rate']}"
        parts2.append(f"音频: {audio}")
    if "bitrate" in info:
        parts2.append(f"码率: {info['bitrate']}")
    return "  ".join(parts1), "  ".join(parts2)


def _parse_ffmpeg_progress(line: str) -> dict | None:
    """Parse ffmpeg progress output line."""
    info = {}
    time_match = re.search(r"time=(\d{2}:\d{2}:\d{2}\.\d{2})", line)
    if time_match:
        info["time"] = time_match.group(1)
    size_match = re.search(r"size=\s*(\d+)(\w+)", line)
    if size_match:
        info["size"] = f"{size_match.group(1)}{size_match.group(2)}"
    speed_match = re.search(r"speed=\s*([\d.]+)x", line)
    if speed_match:
        info["speed"] = f"{speed_match.group(1)}x"
    return info if info else None


def _build_record_cmd(ffmpeg: str, url: str, output: str, duration: float | None = None) -> list[str]:
    """Build the ffmpeg command line for recording a stream.

    `-fflags nobuffer` is intentionally not used: recent ffmpeg builds (e.g.
    2026-07 nightly) make `-c copy` recordings empty or truncated with it
    (0 packets muxed even though input demuxes fine).
    """
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "info", "-stats",
           "-thread_queue_size", "512", "-i", url, "-c", "copy"]
    if url.lower().endswith((".m3u8", ".m3u")):
        cmd[1:1] = ["-allowed_extensions", "ALL", "-allowed_segment_extensions", "ALL", "-extension_picky", "0",
                    "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "2",
                    "-reconnect_max_retries", "5", "-http_persistent", "0"]
    if duration:
        cmd += ["-t", str(duration)]
    cmd += ["-y", output]
    return cmd


# ── Logging handler ──────────────────────────────────────────


class PersistentTextHandler(logging.Handler):
    """Logging handler that writes to both Text widget and log file."""

    def __init__(self, widget: tk.Text, root: tk.Tk):
        super().__init__()
        self._widget = widget
        self._root = root
        self._log_counter = 0
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.log_file = LOG_DIR / f"session_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        if "Using proactor:" in msg:
            return
        self._root._call_ui(self._append_ui, msg)
        self._append_file(msg)

    def _append_ui(self, msg: str) -> None:
        if len(msg) > 8192:
            msg = msg[:8192] + "\n...(日志过长已截断)"
        tag = "log_even" if self._log_counter % 2 == 0 else "log_odd"
        self._log_counter += 1
        self._widget.insert("end", msg + "\n\n", tag)
        self._widget.see("end")

    def reset_counter(self) -> None:
        self._log_counter = 0

    def rotate(self) -> Path:
        """Switch to a fresh session log file and return its path.

        Called after the current log is cleared: the handler re-opens the file
        on every write, so without rotating the next record would immediately
        re-create the file that was just deleted.
        """
        self._log_counter = 0
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        current = self.log_file
        candidate = LOG_DIR / f"session_{stamp}.log"
        index = 1
        # Must differ from the file just deleted even when cleared within the
        # same second.
        while candidate.exists() or candidate == current:
            candidate = LOG_DIR / f"session_{stamp}_{index}.log"
            index += 1
        self.log_file = candidate
        return candidate

    def _append_file(self, msg: str) -> None:
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass


# ── Wheel selector widget ────────────────────────────────────


class WheelSelector(tk.Canvas):
    """Canvas-based wheel/roller selector for numeric values."""

    ITEM_HEIGHT = 32
    VISIBLE_COUNT = 5
    WIDTH = 70
    FONT_SIZE = 14
    COLORS = {
        "bg": "#f0f0f0", "selected": "#4a90d9", "selected_fg": "#ffffff",
        "normal_fg": "#333333", "dim_fg": "#aaaaaa", "line": "#cccccc",
    }

    def __init__(self, parent: tk.Widget, min_val: int = 0, max_val: int = 59, **kwargs):
        height = self.ITEM_HEIGHT * self.VISIBLE_COUNT
        super().__init__(parent, width=self.WIDTH, height=height,
                         highlightthickness=0, bg=self.COLORS["bg"], **kwargs)
        self._min, self._max = min_val, max_val
        self._count = max_val - min_val + 1
        self._value, self._offset = min_val, 0.0
        self._editing, self._edit_entry = False, None
        self._drag_start_y, self._drag_start_offset = 0, 0
        self.bind("<MouseWheel>", self._on_mousewheel)
        self.bind("<Button-4>", self._on_mousewheel)
        self.bind("<Button-5>", self._on_mousewheel)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Double-Button-1>", self._on_double_click)
        self._draw()

    def get(self) -> int:
        return self._value

    def set(self, value: int) -> None:
        self._value = max(self._min, min(self._max, value))
        self._offset = 0.0
        self._draw()

    def _on_mousewheel(self, event: tk.Event) -> None:
        if self._editing:
            return
        if event.num == 4 or (hasattr(event, "delta") and event.delta > 0):
            self._change(-1)
        elif event.num == 5 or (hasattr(event, "delta") and event.delta < 0):
            self._change(1)
        return "break"

    def _on_press(self, event: tk.Event) -> None:
        if self._editing:
            self._on_edit_confirm()
            return
        self._drag_start_y = event.y
        self._drag_start_offset = self._offset

    def _on_drag(self, event: tk.Event) -> None:
        if self._editing:
            return
        self._offset = self._drag_start_offset + (event.y - self._drag_start_y) / self.ITEM_HEIGHT
        self._draw()

    def _on_release(self, event: tk.Event) -> None:
        if self._editing:
            return
        new_value = self._min + ((self._value - round(self._offset) - self._min) % self._count)
        self._value, self._offset = new_value, 0.0
        self._draw()
        self.event_generate("<<WheelChanged>>")

    def _change(self, delta: int) -> None:
        if self._editing:
            return
        self._value = self._min + ((self._value + delta - self._min) % self._count)
        self._offset = 0.0
        self._draw()
        self.event_generate("<<WheelChanged>>")

    def _on_double_click(self, event: tk.Event) -> None:
        if self._editing:
            self._on_edit_confirm()
            return
        mid = self.winfo_height() // 2
        if mid - self.ITEM_HEIGHT // 2 <= event.y <= mid + self.ITEM_HEIGHT // 2:
            self._show_edit_entry()

    def _show_edit_entry(self) -> None:
        self._editing = True
        mid = self.winfo_height() // 2
        self._edit_entry = tk.Entry(self, font=("Microsoft YaHei", self.FONT_SIZE, "bold"),
                                    justify="center", width=3, bg="white", relief="solid", bd=1)
        self._edit_entry.insert(0, f"{self._value:02d}")
        self._edit_entry.select_range(0, tk.END)
        self._edit_entry.place(x=self.WIDTH // 2, y=mid, anchor="center")
        self._edit_entry.focus_set()
        self._edit_entry.bind("<Return>", self._on_edit_confirm)
        self._edit_entry.bind("<Escape>", self._on_edit_cancel)
        self._edit_entry.bind("<FocusOut>", self._on_edit_confirm)

    def _on_edit_confirm(self, event=None) -> None:
        if not self._editing or not self._edit_entry:
            return
        self._editing = False
        try:
            text = self._edit_entry.get().strip()
            if text:
                self._value = max(self._min, min(self._max, int(text)))
        except (ValueError, tk.TclError):
            pass
        self._hide_edit_entry()
        self.event_generate("<<WheelChanged>>")

    def _on_edit_cancel(self, event=None) -> None:
        if not self._editing:
            return
        self._editing = False
        self._hide_edit_entry()

    def _hide_edit_entry(self) -> None:
        if self._edit_entry:
            try:
                self._edit_entry.destroy()
            except tk.TclError:
                pass
            self._edit_entry = None
        self._editing = False
        self._offset = 0.0
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        w, h, mid = self.WIDTH, self.ITEM_HEIGHT * self.VISIBLE_COUNT, self.ITEM_HEIGHT * self.VISIBLE_COUNT // 2
        sel_y1, sel_y2 = mid - self.ITEM_HEIGHT // 2, mid + self.ITEM_HEIGHT // 2
        self.create_rectangle(0, sel_y1, w, sel_y2, fill=self.COLORS["selected"], outline="")
        self.create_line(0, sel_y1, w, sel_y1, fill=self.COLORS["line"])
        self.create_line(0, sel_y2, w, sel_y2, fill=self.COLORS["line"])
        half_visible = self.VISIBLE_COUNT // 2
        buffer = max(self.VISIBLE_COUNT, abs(int(self._offset)) + 2)
        for i in range(-half_visible - buffer, half_visible + buffer + 1):
            item_value = self._min + ((self._value + i - self._min) % self._count)
            y = mid + (i + self._offset) * self.ITEM_HEIGHT
            if y < -self.ITEM_HEIGHT * 2 or y > h + self.ITEM_HEIGHT * 2:
                continue
            if sel_y1 < y < sel_y2:
                color, font = self.COLORS["selected_fg"], ("Microsoft YaHei", self.FONT_SIZE, "bold")
            else:
                dist = abs(y - mid) / (half_visible * self.ITEM_HEIGHT)
                color = self.COLORS["normal_fg"] if dist < 0.5 else self.COLORS["dim_fg"]
                font = ("Microsoft YaHei", self.FONT_SIZE)
            self.create_text(w // 2, y, text=f"{item_value:02d}", fill=color, font=font, anchor="center")


# ── Scrollable frame widget ─────────────────────────────────


class ScrollableFrame(ttk.Frame):
    """Scrollable frame container with mouse wheel support."""

    def __init__(self, parent: tk.Widget, **kwargs):
        super().__init__(parent, **kwargs)
        self._canvas = tk.Canvas(self, highlightthickness=0, bg="#f0f0f0")
        self._scrollbar = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._inner = ttk.Frame(self._canvas)
        self._inner.bind("<Configure>", lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas_window = self._canvas.create_window((0, 0), window=self._inner, anchor="nw")
        # Same debounce as the main window: re-flowing the inner frame for every
        # <Configure> of a dialog drag is more work than one frame allows.
        self._width = 0
        self._width_sync = _DebouncedCall(self, _RESIZE_SETTLE_MS, self._apply_width)
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        self._canvas.configure(yscrollcommand=self._scrollbar.set)
        self._scrollbar.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)
        # Bind wheel events on the owning toplevel rather than bind_all: the
        # toplevel-level binding dies together with the window, while bind_all
        # left three global bindings behind for every dialog ever opened.
        self._toplevel = self.winfo_toplevel()
        self._wheel_bindings: list[tuple[str, str]] = []
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            funcid = self._toplevel.bind(seq, self._on_mousewheel, add="+")
            if funcid:
                self._wheel_bindings.append((seq, funcid))
        self.bind("<Destroy>", self._on_destroy, add="+")

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        self._width_sync.cancel()
        for seq, funcid in self._wheel_bindings:
            try:
                self._toplevel.unbind(seq, funcid)
            except tk.TclError:
                pass
        self._wheel_bindings.clear()

    def _on_canvas_configure(self, event: tk.Event) -> None:
        if self._width <= 0:
            self._apply_width(event.width)   # first paint: no delay
        else:
            self._width_sync(event.width)

    def _apply_width(self, width: int) -> None:
        """Match the inner frame to the canvas width (debounced)."""
        if width <= 1 or width == self._width:
            return
        self._width = width
        try:
            self._canvas.itemconfig(self._canvas_window, width=width)
        except tk.TclError:
            pass

    def _on_mousewheel(self, event: tk.Event) -> None:
        try:
            if not self._canvas.winfo_exists():
                return
            x, y = self._canvas.winfo_pointerxy()
            cx, cy = self._canvas.winfo_rootx(), self._canvas.winfo_rooty()
            cw, ch = self._canvas.winfo_width(), self._canvas.winfo_height()
        except tk.TclError:
            return
        if not (cx <= x < cx + cw and cy <= y < cy + ch):
            return
        if event.num == 4 or (hasattr(event, "delta") and event.delta > 0):
            self._canvas.yview_scroll(-1, "units")
        elif event.num == 5 or (hasattr(event, "delta") and event.delta < 0):
            self._canvas.yview_scroll(1, "units")
        return "break"

    @property
    def inner(self) -> ttk.Frame:
        return self._inner


class _DebouncedCall:
    """Run *fn* once, *delay* ms after the calls stop, with the latest args.

    A window edge drag delivers ~60 ``<Configure>`` events per second, and
    re-flowing the whole panel for each of them costs 100-200 ms here, far
    more than the gap between two events: the queue never catches up and the
    window lags behind the cursor.  While the drag continues the timer is
    pushed back, so the content keeps its previous width (cheap paint only)
    and is re-flowed exactly once, when the drag settles.
    """

    def __init__(self, widget: tk.Misc, delay: int, fn) -> None:
        self._widget = widget
        self._delay = delay
        self._fn = fn
        self._job: str | None = None
        self._args: tuple = ()

    def __call__(self, *args) -> None:
        self._args = args
        if self._job is not None:
            try:
                self._widget.after_cancel(self._job)
            except tk.TclError:
                pass
        self._job = self._widget.after(self._delay, self._run)

    def _run(self) -> None:
        self._job = None
        try:
            self._fn(*self._args)
        except tk.TclError:
            pass

    def cancel(self) -> None:
        if self._job is not None:
            try:
                self._widget.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None


class FlowFrame(ttk.Frame):
    """Left-to-right container that wraps its children onto extra rows.

    Children are created once by the caller and handed to :meth:`add`; the
    frame repositions them with ``place`` when the available width crosses a
    row boundary.  Nothing is destroyed or recreated, which matters because
    the previous implementation rebuilt every player button on each
    ``<Configure>`` and made dragging the window edge stutter.

    ``place`` is used instead of ``grid`` on purpose: grid column widths are
    shared across rows, so a wrapped row would be pushed right by the wider
    cells of the row above and overflow the frame.
    """

    def __init__(self, parent: tk.Widget, **kwargs) -> None:
        super().__init__(parent, **kwargs)
        # Children are placed, not packed, so the frame's height is set by
        # relayout() and never propagated from its slaves.
        self._items: list[tuple[tk.Widget, int, int]] = []
        self._signature: tuple[tuple[int, ...], ...] | None = None
        self.bind("<Configure>", lambda e: self.relayout(), add="+")

    def add(self, widget: tk.Widget, padx: tuple[int, int] = (0, 0)) -> tk.Widget:
        """Register *widget* (created with this frame as master) for flow."""
        self._items.append((widget, padx[0], padx[1]))
        self._signature = None
        return widget

    def clear(self) -> None:
        """Destroy every registered child so the flow can be rebuilt."""
        for widget, _, _ in self._items:
            try:
                if widget.winfo_exists():
                    widget.destroy()
            except tk.TclError:
                pass
        self._items.clear()
        self._signature = None

    def relayout(self, force: bool = False) -> None:
        if force:
            self._signature = None
        if not self._items:
            return
        available = self.winfo_width()
        if available <= 1:
            return

        rows: list[list[int]] = []
        current: list[int] = []
        used = 0
        for idx, (widget, left, right) in enumerate(self._items):
            try:
                width = widget.winfo_reqwidth() + left + right
            except tk.TclError:
                continue
            if current and used + width > available:
                rows.append(current)
                current = []
                used = 0
            current.append(idx)
            used += width
        if current:
            rows.append(current)

        signature = tuple(tuple(r) for r in rows)
        if signature == self._signature and not force:
            return

        y = 0
        for row in rows:
            row_height = 1
            for idx in row:
                try:
                    row_height = max(row_height, self._items[idx][0].winfo_reqheight())
                except tk.TclError:
                    pass
            x = 0
            for idx in row:
                widget, left, right = self._items[idx]
                try:
                    widget.place(x=x + left, y=y)
                    x += widget.winfo_reqwidth() + left + right
                except tk.TclError:
                    pass
            y += row_height
        try:
            self.configure(height=max(y, 1))
        except tk.TclError:
            pass
        self._signature = signature


def _show_toast(parent: tk.Widget, message: str, duration: int = 2000) -> None:
    toast = tk.Toplevel(parent)
    toast.overrideredirect(True)
    lbl = ttk.Label(toast, text=message, padding=(16, 8), background="#333333",
                    foreground="white", font=("Microsoft YaHei", 9))
    lbl.pack()
    toast.update_idletasks()
    pw = parent.winfo_width()
    ph = parent.winfo_height()
    px = parent.winfo_rootx()
    py = parent.winfo_rooty()
    tw = toast.winfo_reqwidth()
    th = toast.winfo_reqheight()
    toast.geometry(f"+{px + pw - tw - 20}+{py + ph - th - 40}")
    toast.after(duration, toast.destroy)
    toast.transient(parent)


def _show_warning(parent: tk.Widget, title: str, message: str) -> None:
    """Show warning dialog centered on parent."""
    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.resizable(True, True)
    dialog.minsize(200, 100)
    dialog.grab_set()

    msg_frame = ttk.Frame(dialog, padding=20)
    msg_frame.pack(fill="both", expand=True)
    text = tk.Text(msg_frame, font=("Microsoft YaHei", 10), wrap="char",
                   width=50, height=4, relief="flat", bd=0, highlightthickness=0)
    text.insert("1.0", message)
    text.configure(state="disabled")
    text.pack(fill="y", expand=True)

    btn_frame = ttk.Frame(dialog, padding=(0, 10, 0, 15))
    btn_frame.pack(fill="x")
    ttk.Button(btn_frame, text="确定", command=dialog.destroy, width=8).pack(side="right", padx=20)

    dialog.update_idletasks()
    dialog_w = max(200, min(dialog.winfo_reqwidth(), 500))
    dialog_h = dialog.winfo_reqheight()
    x = parent.winfo_rootx() + (parent.winfo_width() - dialog_w) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - dialog_h) // 2
    dialog.geometry(f"{dialog_w}x{dialog_h}+{x}+{y}")

    dialog.wait_window()


def _show_error(parent: tk.Widget, title: str, message: str) -> None:
    _show_warning(parent, title, message)


def _ask_yes_no(parent: tk.Widget, title: str, message: str) -> bool:
    """Show yes/no confirmation dialog centered on parent. Returns True if Yes."""
    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.resizable(True, True)
    dialog.minsize(200, 100)
    dialog.grab_set()

    msg_frame = ttk.Frame(dialog, padding=20)
    msg_frame.pack(fill="both", expand=True)
    text = tk.Text(msg_frame, font=("Microsoft YaHei", 10), wrap="char",
                   width=50, height=4, relief="flat", bd=0, highlightthickness=0)
    text.insert("1.0", message)
    text.configure(state="disabled")
    text.pack(fill="y", expand=True)

    btn_frame = ttk.Frame(dialog, padding=(0, 10, 0, 15))
    btn_frame.pack(fill="x")

    result = [False]

    def on_yes():
        result[0] = True
        dialog.destroy()

    def on_no():
        dialog.destroy()

    ttk.Button(btn_frame, text="是", command=on_yes, width=8).pack(side="right", padx=(0, 20))
    ttk.Button(btn_frame, text="否", command=on_no, width=8).pack(side="right", padx=(0, 10))

    dialog.update_idletasks()
    dialog_w = dialog.winfo_width()
    dialog_h = dialog.winfo_height()
    x = parent.winfo_rootx() + (parent.winfo_width() - dialog_w) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - dialog_h) // 2
    dialog.geometry(f"+{x}+{y}")

    dialog.wait_window()
    return result[0]


def _show_protocol_help(parent: tk.Widget) -> None:
    dialog = tk.Toplevel(parent)
    dialog.title("协议说明")
    dialog.configure(bg="#e0e0e0")
    dialog.resizable(True, True)

    text = tk.Text(dialog, wrap="char", bg="#e0e0e0", fg="#000000",
                   font=("Microsoft YaHei", 10), relief="flat",
                   borderwidth=0, padx=4, pady=4)
    text.insert("1.0", _PROTOCOL_HELP)
    text.configure(state="disabled")
    text.pack(fill="both", expand=True)

    dialog.bind("<Escape>", lambda e: dialog.destroy())

    dialog.update_idletasks()
    w = max(280, min(dialog.winfo_reqwidth(), 380))
    h = max(110, min(dialog.winfo_reqheight(), 200))
    x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
    dialog.geometry(f"{w}x{h}+{x}+{y}")

    dialog.transient(parent)
    dialog.grab_set()
    dialog.focus_set()
    dialog.wait_window()


# ── History dialog ────────────────────────────────────────────


class HistoryDialog(tk.Toplevel):
    """Dialog for viewing capture history."""

    def __init__(self, parent: tk.Tk):
        super().__init__(parent)
        self.title("捕获历史")
        self.resizable(True, True)
        self.minsize(500, 400)
        self.grab_set()

        self._parent = parent
        self._records = _load_history()

        # Buttons at bottom
        frm_btn = ttk.Frame(self, padding=8)
        frm_btn.pack(fill="x", side="bottom")
        ttk.Button(frm_btn, text="清空捕获历史", command=self._clear_history).pack(side="left", padx=4)
        self._confirm_btn = ttk.Button(frm_btn, text="加载", command=self._confirm_and_close, width=8, state="disabled")
        self._confirm_btn.pack(side="right", padx=4)
        self._add_to_playlist_btn = ttk.Button(frm_btn, text="加入播放列表", command=self._add_to_playlist, width=10, state="disabled")
        self._add_to_playlist_btn.pack(side="right", padx=4)
        ttk.Button(frm_btn, text="关闭", command=self.destroy, width=8).pack(side="right", padx=4)

        # Treeview
        columns = ("time", "url", "duration", "resolution", "remark")
        self._tree = ttk.Treeview(self, columns=columns, show="headings", selectmode="browse")

        self._tree.heading("time", text="时间")
        self._tree.heading("url", text="URL")
        self._tree.heading("duration", text="时长")
        self._tree.heading("resolution", text="分辨率")
        self._tree.heading("remark", text="备注")

        self._tree.column("time", width=110, minwidth=90)
        self._tree.column("url", width=250, minwidth=150, stretch=True)
        self._tree.column("duration", width=70, minwidth=60)
        self._tree.column("resolution", width=70, minwidth=60)
        self._tree.column("remark", width=100, minwidth=60, stretch=True)

        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")

        self._tree.pack(fill="both", expand=True)

        self._tree.bind("<Double-1>", self._on_double_click)
        self._tree.bind("<Button-3>", self._on_right_click)
        self._tree.bind("<<TreeviewSelect>>", self._on_select)
        self._tree.tag_configure("even", background="#ffffff")
        self._tree.tag_configure("odd", background="#f2f2f2")

        self._load_records()
        self._center_on_parent()
        TreeviewToolTip(self._tree)

    def destroy(self) -> None:
        self._parent.focus_set()
        super().destroy()

    def _center_on_parent(self) -> None:
        self.update_idletasks()
        dialog_w, dialog_h = 600, 450
        p = self._parent
        x = p.winfo_x() + (p.winfo_width() - dialog_w) // 2
        y = p.winfo_y() + (p.winfo_height() - dialog_h) // 2
        self.geometry(f"{dialog_w}x{dialog_h}+{x}+{y}")
        self.lift()
        self.focus()

    def _load_records(self) -> None:
        for item in self._tree.get_children():
            self._tree.delete(item)
        for i, record in enumerate(reversed(self._records)):
            tag = "even" if i % 2 == 0 else "odd"
            self._tree.insert("", "end", values=(
                record["time"],
                record["url"],
                record.get("duration", ""),
                record.get("resolution", ""),
                record.get("remark", ""),
            ), tags=(str(record["id"]), tag))

    def _get_selected_record(self) -> dict | None:
        selection = self._tree.selection()
        if not selection:
            return None
        item = self._tree.item(selection[0])
        record_id = int(item["tags"][0])
        for record in self._records:
            if record["id"] == record_id:
                return record
        return None

    def _on_select(self, event=None) -> None:
        record = self._get_selected_record()
        state = "normal" if record else "disabled"
        self._confirm_btn.configure(state=state)
        self._add_to_playlist_btn.configure(state=state)

    def _on_double_click(self, event: tk.Event) -> None:
        record = self._get_selected_record()
        if record:
            self._parent._load_url_to_ui(record["url"], record_history=False, remark=record.get("remark", ""), from_history=True)
            self.destroy()

    def _confirm_and_close(self) -> None:
        record = self._get_selected_record()
        if record:
            self._parent._load_url_to_ui(record["url"], record_history=False, remark=record.get("remark", ""), from_history=True)
            self.destroy()

    def _on_right_click(self, event: tk.Event) -> None:
        selection = self._tree.identify_row(event.y)
        if not selection:
            return
        self._tree.selection_set(selection)
        record = self._get_selected_record()
        if not record:
            return

        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="复制 URL", command=self._copy_selected_url)
        menu.add_command(label="修改备注", command=self._edit_remark_selected)
        menu.add_command(label="录制", command=self._record_selected_url)
        menu.add_command(label="加入播放列表", command=self._add_to_playlist)

        players = self._parent._config.get("players", {})
        if players:
            play_menu = tk.Menu(menu, tearoff=0)
            for name in players:
                play_menu.add_command(
                    label=f"▶ {name}",
                    command=lambda n=name, u=record["url"]: self._parent._play_single_url(n, u)
                )
            menu.add_cascade(label="播放", menu=play_menu)

        menu.add_command(label="删除", command=self._delete_selected)

        menu.tk_popup(event.x_root, event.y_root)

    def _copy_selected_url(self) -> None:
        selection = self._tree.selection()
        if not selection:
            return
        item = self._tree.item(selection[0])
        record_id = int(item["tags"][0])
        for record in self._records:
            if record["id"] == record_id:
                self.clipboard_clear()
                self.clipboard_append(record["url"])
                _show_toast(self, "URL 已复制到剪贴板")
                break

    def _delete_selected(self) -> None:
        selection = self._tree.selection()
        if not selection:
            return
        item = self._tree.item(selection[0])
        record_id = int(item["tags"][0])
        with _HISTORY_LOCK:
            # Re-read from disk so entries added in the meantime are not lost.
            records = [r for r in _load_history() if r["id"] != record_id]
            _save_history(records)
            self._records = records
        self._load_records()

    def _add_to_playlist(self) -> None:
        record = self._get_selected_record()
        if not record:
            return
        self._parent._append_to_playlist(record["url"], record.get("remark", ""))
        _show_toast(self, "已加入播放列表")

    def _edit_remark_selected(self) -> None:
        record = self._get_selected_record()
        if not record:
            return
        dialog = tk.Toplevel(self)
        dialog.title("修改备注")
        dialog.resizable(False, False)

        frm = ttk.Frame(dialog, padding=16)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="备注:").pack(anchor="w")
        var = tk.StringVar(value=record.get("remark", ""))
        entry = ttk.Entry(frm, textvariable=var, width=40)
        entry.pack(fill="x", pady=(4, 12))
        entry.focus_set()
        entry.selection_range(0, "end")

        btn_frm = ttk.Frame(frm)
        btn_frm.pack(fill="x")
        result = [False]

        def on_save() -> None:
            remark_text = var.get().strip()
            with _HISTORY_LOCK:
                records = _load_history()
                for rec in records:
                    if rec["id"] == record["id"]:
                        rec["remark"] = remark_text
                        break
                _save_history(records)
                self._records = records
            self._load_records()
            result[0] = True
            dialog.destroy()

        def on_cancel() -> None:
            dialog.destroy()

        ttk.Button(btn_frm, text="取消", command=on_cancel, width=8).pack(side="right", padx=(4, 0))
        ttk.Button(btn_frm, text="保存", command=on_save, width=8).pack(side="right")
        entry.bind("<Return>", lambda e: on_save())

        dialog.update_idletasks()
        dw, dh = dialog.winfo_width(), dialog.winfo_height()
        x = self.winfo_rootx() + (self.winfo_width() - dw) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dh) // 2
        dialog.geometry(f"+{x}+{y}")
        dialog.grab_set()
        dialog.wait_window()
        self.grab_set()
        self.focus_set()

    def _record_selected_url(self) -> None:
        record = self._get_selected_record()
        if not record:
            return
        url = record["url"]
        self._parent._load_url_to_ui(url, record_history=False, remark=record.get("remark", ""), from_history=True)
        save_dir = self._parent._save_dir_var.get().strip()
        dialog = RecordDialog(self._parent, url, save_dir)
        result = dialog.show()
        if result:
            rec_url, output, duration = result
            self.destroy()
            self._parent._start_recording(rec_url, output, duration)

    def _clear_history(self) -> None:
        if _ask_yes_no(self, "确认", "确定要清空所有捕获历史记录吗？"):
            with _HISTORY_LOCK:
                _save_history([])
                self._records = []
            self._load_records()


# ── Playlist dialog ────────────────────────────────────────────


class PlaylistDialog(tk.Toplevel):
    """Dialog for viewing and managing the playback queue."""

    def __init__(self, parent: tk.Tk):
        super().__init__(parent)
        self.title("播放列表")
        self.resizable(True, True)
        self.minsize(600, 400)
        self.grab_set()

        self._parent = parent

        # ── Bottom button row (pack first so Treeview fills remaining space) ──
        frm_btn = ttk.Frame(self, padding=8)
        frm_btn.pack(fill="x", side="bottom")

        self._auto_dedupe_var = tk.BooleanVar(value=self._parent._config.get("auto_dedupe", False))
        ttk.Checkbutton(frm_btn, text="自动去重", variable=self._auto_dedupe_var,
                        command=self._on_auto_dedupe_toggle).pack(side="left", padx=4)
        ttk.Button(frm_btn, text="去重", command=self._on_dedupe_click, width=8).pack(side="left", padx=4)
        ttk.Button(frm_btn, text="添加当前URL", command=self._on_add_current, width=10).pack(side="left", padx=4)
        self._ipc_mode_btn = ttk.Button(frm_btn, text="", width=16, command=self._on_toggle_ipc_mode)
        self._ipc_mode_btn.pack(side="left", padx=(12, 4))
        ToolTip(self._ipc_mode_btn, lambda: _IPC_MODE_HINT)
        self._refresh_ipc_mode_button()
        ttk.Button(frm_btn, text="清空", command=self._on_clear_click, width=8).pack(side="right", padx=4)
        ttk.Button(frm_btn, text="关闭", command=self.destroy, width=8).pack(side="right", padx=4)

        # ── Treeview ──
        columns = ("idx", "time", "url", "duration", "resolution", "remark")
        self._tree = ttk.Treeview(self, columns=columns, show="headings", selectmode="browse")

        self._tree.heading("idx", text="#")
        self._tree.heading("time", text="时间")
        self._tree.heading("url", text="URL")
        self._tree.heading("duration", text="时长")
        self._tree.heading("resolution", text="分辨率")
        self._tree.heading("remark", text="备注")

        self._tree.column("idx", width=40, minwidth=30, stretch=False)
        self._tree.column("time", width=110, minwidth=90, stretch=False)
        self._tree.column("url", width=250, minwidth=150, stretch=True)
        self._tree.column("duration", width=70, minwidth=60, stretch=False)
        self._tree.column("resolution", width=70, minwidth=60, stretch=False)
        self._tree.column("remark", width=100, minwidth=60, stretch=True)

        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self._tree.pack(fill="both", expand=True)

        self._tree.bind("<Double-1>", self._on_double_click)
        self._tree.bind("<Button-3>", self._on_right_click)
        self._tree.tag_configure("even", background="#ffffff")
        self._tree.tag_configure("odd", background="#f2f2f2")
        TreeviewToolTip(self._tree)

        self._load_list()
        self._center_on_parent()

    def destroy(self) -> None:
        self._parent.focus_set()
        super().destroy()

    def _center_on_parent(self) -> None:
        self.update_idletasks()
        dialog_w, dialog_h = 700, 450
        p = self._parent
        x = p.winfo_x() + (p.winfo_width() - dialog_w) // 2
        y = p.winfo_y() + (p.winfo_height() - dialog_h) // 2
        self.geometry(f"{dialog_w}x{dialog_h}+{x}+{y}")
        self.lift()
        self.focus()

    def _load_list(self) -> None:
        for item in self._tree.get_children():
            self._tree.delete(item)
        items = self._parent._playlist_urls
        for i, entry in enumerate(items):
            tag = "even" if i % 2 == 0 else "odd"
            self._tree.insert("", "end", values=(
                i + 1,
                entry.get("time", ""),
                entry.get("url", ""),
                entry.get("duration", ""),
                entry.get("resolution", ""),
                entry.get("remark", ""),
            ), tags=(tag,))
        self.title(f"播放列表 ({len(items)})")

    def _get_selected_url(self) -> str | None:
        selection = self._tree.selection()
        if not selection:
            return None
        item = self._tree.item(selection[0])
        return item["values"][2] if len(item["values"]) > 2 else None

    def _get_selected_index(self) -> int | None:
        selection = self._tree.selection()
        if not selection:
            return None
        item = self._tree.item(selection[0])
        idx = item["values"][0]
        return int(idx) - 1 if idx else None

    def _on_double_click(self, event: tk.Event) -> None:
        url = self._get_selected_url()
        if not url:
            return
        self.clipboard_clear()
        self.clipboard_append(url)
        _show_toast(self, "URL 已复制到剪贴板")

    def _on_right_click(self, event: tk.Event) -> None:
        row = self._tree.identify_row(event.y)
        if not row:
            return
        self._tree.selection_set(row)

        url = self._get_selected_url()
        if not url:
            return

        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="复制 URL", command=self._on_copy_url)
        menu.add_command(label="修改备注", command=self._on_edit_remark)
        menu.add_command(label="录制", command=self._on_record_url)

        players = self._parent._config.get("players", {})
        if players:
            play_menu = tk.Menu(menu, tearoff=0)
            for name in players:
                play_menu.add_command(
                    label=f"▶ {name}",
                    command=lambda n=name: self._parent._play_single_url(n, url)
                )
            menu.add_cascade(label="播放", menu=play_menu)

        menu.add_separator()
        menu.add_command(label="删除", command=self._on_delete_selected)

        menu.tk_popup(event.x_root, event.y_root)

    def _on_copy_url(self) -> None:
        url = self._get_selected_url()
        if url:
            self.clipboard_clear()
            self.clipboard_append(url)
            _show_toast(self, "URL 已复制到剪贴板")

    def _on_edit_remark(self) -> None:
        idx = self._get_selected_index()
        if idx is None or idx < 0 or idx >= len(self._parent._playlist_urls):
            return
        entry = self._parent._playlist_urls[idx]
        dialog = tk.Toplevel(self)
        dialog.title("修改备注")
        dialog.resizable(False, False)
        frm = ttk.Frame(dialog, padding=16)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="备注:").pack(anchor="w")
        var = tk.StringVar(value=entry.get("remark", ""))
        entry_w = ttk.Entry(frm, textvariable=var, width=40)
        entry_w.pack(fill="x", pady=(4, 12))
        entry_w.focus_set()
        entry_w.selection_range(0, "end")

        btn_frm = ttk.Frame(frm)
        btn_frm.pack(fill="x")

        def on_save():
            entry["remark"] = var.get().strip()
            self._load_list()
            dialog.destroy()

        ttk.Button(btn_frm, text="取消", command=dialog.destroy, width=8).pack(side="right", padx=(4, 0))
        ttk.Button(btn_frm, text="保存", command=on_save, width=8).pack(side="right")
        entry_w.bind("<Return>", lambda e: on_save())

        dialog.update_idletasks()
        dw, dh = dialog.winfo_width(), dialog.winfo_height()
        x = self.winfo_rootx() + (self.winfo_width() - dw) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dh) // 2
        dialog.geometry(f"+{x}+{y}")
        dialog.grab_set()
        dialog.wait_window()

    def _on_record_url(self) -> None:
        url = self._get_selected_url()
        if not url:
            return
        self._parent._load_url_to_ui(url, record_history=False, from_history=True)
        save_dir = self._parent._save_dir_var.get().strip()
        dialog = RecordDialog(self._parent, url, save_dir)
        result = dialog.show()
        if result:
            rec_url, output, duration = result
            self._parent._start_recording(rec_url, output, duration)

    def _on_delete_selected(self) -> None:
        idx = self._get_selected_index()
        if idx is None or idx < 0 or idx >= len(self._parent._playlist_urls):
            return
        self._parent._playlist_urls.pop(idx)
        self._parent._write_playlist()
        self._parent._update_player_button_states()
        self._load_list()

    def _on_dedupe_click(self) -> None:
        items = self._parent._playlist_urls
        seen = set()
        deduped = []
        for entry in items:
            u = entry["url"]
            if u not in seen:
                seen.add(u)
                deduped.append(entry)
        removed = len(items) - len(deduped)
        if removed == 0:
            _show_toast(self, "无重复项")
            return
        self._parent._playlist_urls = deduped
        self._parent._write_playlist()
        self._parent._update_player_button_states()
        self._load_list()
        _show_toast(self, f"已移除 {removed} 个重复项")

    def _on_add_current(self) -> None:
        url = self._parent._url_var.get().strip()
        if not url:
            _show_toast(self, "无可用 URL")
            return
        if any(i["url"] == url for i in self._parent._playlist_urls):
            _show_toast(self, "URL 已在播放列表中")
            return
        self._parent._append_to_playlist(url)
        self._load_list()
        _show_toast(self, "已添加当前 URL")

    def _on_clear_click(self) -> None:
        self._parent._clear_queue()
        self._parent._playlist_urls.clear()
        self._parent._playlist_path = None
        self._parent._ipc_pipe = ""
        self._parent._update_player_button_states()
        self.destroy()

    def _on_auto_dedupe_toggle(self) -> None:
        self._parent._config["auto_dedupe"] = self._auto_dedupe_var.get()
        _save_config(self._parent._config)

    def _refresh_ipc_mode_button(self) -> None:
        mode = _get_ipc_mode(self._parent._config)
        self._ipc_mode_btn.configure(
            text="播放方式: 立即切换" if mode == IPC_MODE_REPLACE else "播放方式: 排队"
        )

    def _on_toggle_ipc_mode(self) -> None:
        current = _get_ipc_mode(self._parent._config)
        new_mode = IPC_MODE_REPLACE if current == IPC_MODE_QUEUE else IPC_MODE_QUEUE
        self._parent._config["mpv_ipc_mode"] = new_mode
        _save_config(self._parent._config)
        self._refresh_ipc_mode_button()
        log.info("mpv 点播方式切换为: %s", new_mode)
        if new_mode == IPC_MODE_REPLACE:
            _show_toast(self, "点播将立即切换，不排队")
        else:
            _show_toast(self, "点播将加入队列，播完当前内容再播")


# ── Record dialog ──────────────────────────────────────────────


class RecordDialog(tk.Toplevel):
    """Dialog for configuring recording settings."""

    def __init__(self, parent: tk.Tk, url: str, save_dir: str,
                 stream_duration: float | None = None):
        super().__init__(parent)
        self._parent = parent
        self.title("录制设置")
        self.resizable(True, True)
        self.minsize(400, 300)
        self.grab_set()

        self._url = url
        self._stream_duration = stream_duration
        self._result: tuple[str, str, float | None] | None = None
        self._fmt_touched = False
        self._dur_touched = False
        self._dur_token = 0
        self._dur_queue: queue.Queue[tuple[float | None, str, int]] = queue.Queue()
        self._url_var = tk.StringVar(value=url)
        self._poll_after_id = self.after(50, self._poll_duration_queue)

        default_ext, _ = _detect_format(url)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        pad = dict(padx=8, pady=4)

        # Buttons at bottom
        frm_btn = ttk.Frame(self, padding=8)
        frm_btn.pack(fill="x", side="bottom")
        ttk.Button(frm_btn, text="取消", command=self._cancel, width=10).pack(side="right", padx=4)
        ttk.Button(frm_btn, text="开始录制", command=self._confirm, width=10).pack(side="right", padx=4)

        # Scrollable content
        scrollable = ScrollableFrame(self)
        scrollable.pack(fill="both", expand=True)
        content = scrollable.inner

        # Stream URL
        frm_url = ttk.LabelFrame(content, text="流地址", padding=8)
        frm_url.pack(fill="x", **pad)
        self._url_entry = ttk.Entry(frm_url, textvariable=self._url_var, width=40)
        self._url_entry.pack(fill="x")
        self._url_entry.bind("<KeyRelease>", self._on_url_changed)
        _bind_enter_commit(self._url_entry, self._on_url_changed)

        # Save directory
        frm_dir = ttk.LabelFrame(content, text="存储目录", padding=8)
        frm_dir.pack(fill="x", **pad)
        row_dir = ttk.Frame(frm_dir)
        row_dir.pack(fill="x")
        self._dir_var = tk.StringVar(value=save_dir)
        self._dir_entry = ttk.Entry(row_dir, textvariable=self._dir_var, width=40)
        self._dir_entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
        _bind_enter_commit(self._dir_entry, self._commit_dir)
        ttk.Button(row_dir, text="浏览...", command=self._browse_dir).pack(side="left")

        # Filename
        frm_name = ttk.LabelFrame(content, text="文件名", padding=8)
        frm_name.pack(fill="x", **pad)
        self._name_var = tk.StringVar(value=f"recording_{timestamp}")
        self._name_entry = ttk.Entry(frm_name, textvariable=self._name_var, width=40)
        self._name_entry.pack(fill="x")
        self._name_entry.bind("<KeyRelease>", lambda e: self._update_preview())
        _bind_enter_commit(self._name_entry, self._commit_name)

        # Format
        frm_fmt = ttk.LabelFrame(content, text="格式 (自动匹配，可手动切换)", padding=8)
        frm_fmt.pack(fill="x", **pad)
        self._ext_var = tk.StringVar(value=default_ext)
        self._fmt_combo = ttk.Combobox(frm_fmt, state="readonly", width=30)
        self._fmt_combo["values"] = [f"{name} ({ext})" for ext, name in _FORMAT_OPTIONS]
        for i, (ext, _) in enumerate(_FORMAT_OPTIONS):
            if ext == default_ext:
                self._fmt_combo.current(i)
                break
        self._fmt_combo.pack(fill="x")
        self._fmt_combo.bind("<<ComboboxSelected>>", self._on_format_change)

        # Duration with wheels
        frm_dur = ttk.LabelFrame(content, text="录制时长", padding=8)
        frm_dur.pack(fill="x", **pad)

        frm_presets = ttk.Frame(frm_dur)
        frm_presets.pack(fill="x", pady=(0, 8))
        for text, h, m, s in [("30分钟", 0, 30, 0), ("1小时", 1, 0, 0), ("2小时", 2, 0, 0), ("不限", 0, 0, 0)]:
            ttk.Button(frm_presets, text=text, command=lambda h=h, m=m, s=s: self._set_duration(h, m, s), width=8).pack(side="left", padx=2)

        frm_wheels = ttk.Frame(frm_dur)
        frm_wheels.pack(fill="x", pady=4)
        self._hour_wheel = WheelSelector(frm_wheels, min_val=0, max_val=99)
        self._hour_wheel.pack(side="left", padx=(20, 2))
        ttk.Label(frm_wheels, text="时", font=("Microsoft YaHei", 10)).pack(side="left", padx=(0, 10))
        self._min_wheel = WheelSelector(frm_wheels, min_val=0, max_val=59)
        self._min_wheel.pack(side="left", padx=2)
        ttk.Label(frm_wheels, text="分", font=("Microsoft YaHei", 10)).pack(side="left", padx=(0, 10))
        self._sec_wheel = WheelSelector(frm_wheels, min_val=0, max_val=59)
        self._sec_wheel.pack(side="left", padx=2)
        ttk.Label(frm_wheels, text="秒", font=("Microsoft YaHei", 10)).pack(side="left", padx=(0, 20))

        for wheel in (self._hour_wheel, self._min_wheel, self._sec_wheel):
            wheel.bind("<<WheelChanged>>", lambda e: self._on_wheel_change())

        # Fixed-height hints
        frm_hints = ttk.Frame(frm_dur, height=80)
        frm_hints.pack(fill="x", pady=(4, 0))
        frm_hints.pack_propagate(False)

        self._duration_display_var = tk.StringVar(value="00:00:00 (不限)")
        ttk.Label(frm_hints, textvariable=self._duration_display_var, font=("Microsoft YaHei", 10, "bold")).pack(fill="x")
        self._hint_var = tk.StringVar()
        if stream_duration and stream_duration > 0:
            self._hint_var.set(f"流时长: {_format_duration(stream_duration)}")
        else:
            self._hint_var.set("直播流，建议设置时长")
        ttk.Label(frm_hints, textvariable=self._hint_var, foreground="gray").pack(fill="x")
        self._warning_var = tk.StringVar()
        ttk.Label(frm_hints, textvariable=self._warning_var, foreground="orange", wraplength=450).pack(fill="x")

        # Preview
        frm_preview = ttk.LabelFrame(content, text="完整路径预览", padding=8)
        frm_preview.pack(fill="x", **pad)
        self._preview_var = tk.StringVar()
        ttk.Label(frm_preview, textvariable=self._preview_var, foreground="gray", wraplength=450).pack(fill="x")

        self._update_preview()
        self._update_duration_display()

        if stream_duration and stream_duration > 0:
            self._prefill_duration(stream_duration)

        self.update_idletasks()
        dialog_w, dialog_h = 400, 600
        x = parent.winfo_x() + (parent.winfo_width() - dialog_w) // 2
        y = parent.winfo_y() + (parent.winfo_height() - dialog_h) // 2
        self.geometry(f"{dialog_w}x{dialog_h}+{x}+{y}")

        if not (stream_duration and stream_duration > 0):
            self._on_url_changed()

    def _set_duration(self, h: int, m: int, s: int) -> None:
        self._hour_wheel.set(h)
        self._min_wheel.set(m)
        self._sec_wheel.set(s)
        self._on_wheel_change()

    def _on_wheel_change(self) -> None:
        self._dur_touched = True
        self._update_duration_display()
        self._update_duration_warning()

    def _update_duration_display(self) -> None:
        h, m, s = self._hour_wheel.get(), self._min_wheel.get(), self._sec_wheel.get()
        self._duration_display_var.set("00:00:00 (不限)" if h == m == s == 0 else f"{h:02d}:{m:02d}:{s:02d}")

    def _update_duration_warning(self) -> None:
        selected = self._hour_wheel.get() * 3600 + self._min_wheel.get() * 60 + self._sec_wheel.get()
        if selected > 0 and self._stream_duration and self._stream_duration > 0 and selected > self._stream_duration:
            self._warning_var.set(f"⚠ 超过流时长({_format_duration(self._stream_duration)})，将自动停止")
        else:
            self._warning_var.set("")

    def _browse_dir(self) -> None:
        path = filedialog.askdirectory(title="选择存储目录", initialdir=self._dir_var.get(), parent=self)
        if path:
            self._dir_var.set(path)
            self._update_preview()

    def _commit_dir(self) -> None:
        """Trim the directory typed by hand and refresh the path preview."""
        self._dir_var.set(self._dir_var.get().strip())
        self._update_preview()

    def _commit_name(self) -> None:
        """Trim the file name typed by hand and refresh the path preview."""
        self._name_var.set(self._name_var.get().strip())
        self._update_preview()

    def _on_format_change(self, event: tk.Event) -> None:
        self._fmt_touched = True
        idx = self._fmt_combo.current()
        if 0 <= idx < len(_FORMAT_OPTIONS):
            self._ext_var.set(_FORMAT_OPTIONS[idx][0])
            self._update_preview()

    def _on_url_changed(self, event: tk.Event = None) -> None:
        new_url = self._url_var.get().strip()
        if not new_url:
            return
        if not self._fmt_touched:
            new_ext, _ = _detect_format(new_url)
            self._ext_var.set(new_ext)
            for i, (ext, _) in enumerate(_FORMAT_OPTIONS):
                if ext == new_ext:
                    self._fmt_combo.current(i)
                    break
            self._update_preview()
        self._hint_var.set("获取流时长中...")
        self._warning_var.set("")
        self._dur_token += 1
        token = self._dur_token
        threading.Thread(target=self._refresh_duration, args=(new_url, token), daemon=True).start()

    def _refresh_duration(self, url: str, token: int) -> None:
        duration, text = _get_duration(url)
        if token == self._dur_token:
            self._dur_queue.put((duration, text, token))

    def _poll_duration_queue(self) -> None:
        try:
            while True:
                duration, text, token = self._dur_queue.get_nowait()
                self._apply_duration(duration, text, token)
        except queue.Empty:
            pass
        except Exception:
            log.debug("RecordDialog: duration poll failed", exc_info=True)
        self._poll_after_id = self.after(50, self._poll_duration_queue)

    def _apply_duration(self, duration: float | None, text: str, token: int) -> None:
        if token != self._dur_token:
            return
        self._stream_duration = duration
        if duration and duration > 0:
            self._hint_var.set(f"流时长: {text}")
            if not self._dur_touched:
                self._prefill_duration(duration)
        else:
            self._hint_var.set("直播流，建议设置时长")
        self._update_duration_warning()

    def _prefill_duration(self, seconds: float) -> None:
        total = int(seconds)
        if total >= 360000:
            return
        self._hour_wheel.set(total // 3600)
        self._min_wheel.set(total % 3600 // 60)
        self._sec_wheel.set(total % 60)
        self._update_duration_display()
        self._update_duration_warning()

    def _update_preview(self) -> None:
        save_dir, filename, ext = self._dir_var.get().strip(), self._name_var.get().strip(), self._ext_var.get()
        self._preview_var.set(str(Path(save_dir) / f"{filename}{ext}") if save_dir and filename else "(请填写存储目录和文件名)")

    def _cancel(self) -> None:
        self._result = None
        self.destroy()

    def _confirm(self) -> None:
        save_dir, filename, ext = self._dir_var.get().strip(), self._name_var.get().strip(), self._ext_var.get()
        url = self._url_var.get().strip()
        if not url:
            _show_warning(self, "提示", "请输入流地址")
            return
        if not save_dir:
            _show_warning(self, "提示", "请选择存储目录")
            return
        if not filename:
            _show_warning(self, "提示", "请输入文件名")
            return
        h, m, s = self._hour_wheel.get(), self._min_wheel.get(), self._sec_wheel.get()
        duration = h * 3600 + m * 60 + s if (h > 0 or m > 0 or s > 0) else None
        Path(save_dir).mkdir(parents=True, exist_ok=True)
        self._result = (url, str(Path(save_dir) / f"{filename}{ext}"), duration)
        self.destroy()

    def show(self) -> tuple[str, str, float | None] | None:
        self.wait_window()
        return self._result

    def destroy(self) -> None:
        self._parent.focus_set()
        try:
            self.after_cancel(self._poll_after_id)
        except Exception:
            pass
        super().destroy()


class PlayerDialog(tk.Toplevel):
    """Dialog for managing media players (add/edit/delete)."""

    def __init__(self, parent: App, players: dict, on_change: callable):
        super().__init__(parent)
        self.title("播放器管理")
        self.minsize(480, 320)
        self.resizable(True, True)
        self.grab_set()

        self._parent = parent
        self._players = players
        self._on_change = on_change

        self._build_ui()
        self._refresh_list()
        self._center_on_parent()

    def destroy(self) -> None:
        self._parent.focus_set()
        super().destroy()

    def _center_on_parent(self) -> None:
        self.update_idletasks()
        pw = self._parent.winfo_width()
        ph = self._parent.winfo_height()
        px = self._parent.winfo_x()
        py = self._parent.winfo_y()
        dw = self.winfo_width()
        dh = self.winfo_height()
        w = max(500, dw)
        h = max(360, dh)
        self.geometry(f"{w}x{h}+{px + (pw - w) // 2}+{py + (ph - dh) // 2}")

    def _build_ui(self) -> None:
        pad = dict(padx=8, pady=4)

        columns = ("name", "path", "protocol", "manual_protocol")
        self._tree = ttk.Treeview(self, columns=columns, show="headings", selectmode="browse")

        self._tree.heading("name", text="名称")
        self._tree.heading("path", text="路径")
        self._tree.heading("protocol", text="自动协议")
        self._tree.heading("manual_protocol", text="手动协议")

        self._tree.column("name", width=100, minwidth=80)
        self._tree.column("path", width=220, minwidth=160, stretch=True)
        self._tree.column("protocol", width=80, minwidth=60)
        self._tree.column("manual_protocol", width=80, minwidth=60)

        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")

        self._tree.pack(fill="both", expand=True, **pad)

        self._tree.bind("<<TreeviewSelect>>", self._on_select)
        self._tree.bind("<Button-3>", self._on_right_click)
        self._tree.tag_configure("even", background="#ffffff")
        self._tree.tag_configure("odd", background="#f2f2f2")
        TreeviewToolTip(self._tree)

        self._path_var = tk.StringVar()
        ttk.Label(self, textvariable=self._path_var, foreground="gray", wraplength=600).pack(fill="x", padx=8, pady=(0, 4))

        btn_bottom = ttk.Frame(self)
        btn_bottom.pack(fill="x", **pad)
        btn_bottom.columnconfigure((0, 1, 2, 3, 4, 5), weight=1)
        ttk.Button(btn_bottom, text="删除", command=self._remove).grid(row=0, column=0, padx=2)
        ttk.Button(btn_bottom, text="上移", command=self._move_up).grid(row=0, column=1, padx=2)
        ttk.Button(btn_bottom, text="下移", command=self._move_down).grid(row=0, column=2, padx=2)
        ttk.Button(btn_bottom, text="添加", command=self._add).grid(row=0, column=3, padx=2)
        ttk.Button(btn_bottom, text="编辑", command=self._edit).grid(row=0, column=4, padx=2)
        ttk.Button(btn_bottom, text="关闭", command=self.destroy).grid(row=0, column=5, padx=2)

    def _refresh_list(self) -> None:
        for item in self._tree.get_children():
            self._tree.delete(item)
        for i, (name, entry) in enumerate(self._players.items()):
            tag = "even" if i % 2 == 0 else "odd"
            self._tree.insert("", "end", values=(
                name,
                _get_player_path(entry),
                _get_player_protocol(entry),
                _get_player_manual_protocol(entry),
            ), tags=(tag,)) 
        children = self._tree.get_children()
        if children:
            self._tree.selection_set(children[0])
            self._on_select()

    def _on_select(self, event=None) -> None:
        sel = self._tree.selection()
        if sel:
            values = self._tree.item(sel[0], "values")
            self._path_var.set(f"路径: {values[1]}")
        else:
            self._path_var.set("")

    def _on_right_click(self, event: tk.Event) -> None:
        row = self._tree.identify_row(event.y)
        if not row:
            return
        self._tree.selection_set(row)
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="编辑", command=self._edit)
        menu.add_separator()
        menu.add_command(label="上移", command=self._move_up)
        menu.add_command(label="下移", command=self._move_down)
        menu.add_separator()
        menu.add_command(label="删除", command=self._remove)
        menu.add_separator()
        menu.add_command(label="复制路径", command=self._copy_selected_path)
        menu.tk_popup(event.x_root, event.y_root)

    def _copy_selected_path(self) -> None:
        sel = self._tree.selection()
        if sel:
            values = self._tree.item(sel[0], "values")
            self.clipboard_clear()
            self.clipboard_append(values[1])

    def _move_up(self) -> None:
        sel = self._tree.selection()
        if not sel:
            _show_warning(self, "提示", "请先选择一个播放器")
            return
        name = self._tree.item(sel[0], "values")[0]
        keys = list(self._players.keys())
        idx = keys.index(name)
        if idx == 0:
            return
        keys[idx], keys[idx - 1] = keys[idx - 1], keys[idx]
        ordered = {k: self._players[k] for k in keys}
        self._players.clear()
        self._players.update(ordered)
        _save_config(self._parent._config)
        self._refresh_list()
        for item in self._tree.get_children():
            if self._tree.item(item, "values")[0] == name:
                self._tree.selection_set(item)
                break
        self._on_change()

    def _move_down(self) -> None:
        sel = self._tree.selection()
        if not sel:
            _show_warning(self, "提示", "请先选择一个播放器")
            return
        name = self._tree.item(sel[0], "values")[0]
        keys = list(self._players.keys())
        idx = keys.index(name)
        if idx == len(keys) - 1:
            return
        keys[idx], keys[idx + 1] = keys[idx + 1], keys[idx]
        ordered = {k: self._players[k] for k in keys}
        self._players.clear()
        self._players.update(ordered)
        _save_config(self._parent._config)
        self._refresh_list()
        for item in self._tree.get_children():
            if self._tree.item(item, "values")[0] == name:
                self._tree.selection_set(item)
                break
        self._on_change()

    def _add(self) -> None:
        path = filedialog.askopenfilename(
            title="选择播放器可执行文件",
            filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")],
            parent=self,
        )
        if not path:
            return

        dialog = tk.Toplevel(self)
        dialog.title("添加播放器")
        dialog.minsize(420, 0)
        dialog.resizable(True, True)

        ttk.Label(dialog, text="播放器名称:").pack(anchor="w", padx=10, pady=(6, 2))
        name_var = tk.StringVar(value=Path(path).stem)
        name_entry = ttk.Entry(dialog, textvariable=name_var)
        name_entry.pack(fill="x", padx=10, pady=(0, 2))
        name_entry.select_range(0, tk.END)
        name_entry.focus_set()

        ttk.Label(dialog, text="播放器路径:").pack(anchor="w", padx=10, pady=(2, 2))
        row_path = ttk.Frame(dialog)
        row_path.pack(fill="x", padx=10)
        path_var = tk.StringVar(value=path)
        path_entry = ttk.Entry(row_path, textvariable=path_var)
        path_entry.pack(side="left", fill="x", expand=True)

        def browse_path():
            p = filedialog.askopenfilename(
                title="选择播放器可执行文件",
                filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")],
                parent=dialog,
            )
            if p:
                path_var.set(p)

        ttk.Button(row_path, text="浏览...", command=browse_path).pack(side="left", padx=(4, 0))

        ttk.Label(dialog, text="自动播放协议:").pack(anchor="w", padx=10, pady=(4, 2))
        protocol_var = tk.StringVar(value="auto")
        protocol_combo = ttk.Combobox(dialog, textvariable=protocol_var,
                                       values=["auto", "direct", "playlist", "ipc"],
                                       state="readonly")
        protocol_combo.pack(fill="x", padx=10, pady=(0, 2))
        protocol_combo.bind("<<ComboboxSelected>>", lambda e: dialog.after(10, dialog.focus_set))

        ttk.Label(dialog, text="手动播放协议:").pack(anchor="w", padx=10, pady=(4, 2))
        manual_var = tk.StringVar(value="auto")
        manual_combo = ttk.Combobox(dialog, textvariable=manual_var,
                                     values=["auto", "direct", "playlist", "ipc"],
                                     state="readonly")
        manual_combo.pack(fill="x", padx=10, pady=(0, 2))
        manual_combo.bind("<<ComboboxSelected>>", lambda e: dialog.after(10, dialog.focus_set))

        ttk.Button(dialog, text="协议说明",
                   command=lambda: _show_protocol_help(dialog)
                   ).pack(fill="x", padx=10, pady=(6, 0))

        result = {"ok": False}

        def confirm():
            name = name_var.get().strip()
            if not name:
                _show_warning(dialog, "提示", "播放器名称不能为空")
                return
            result["ok"] = True
            result["name"] = name
            result["path"] = path_var.get().strip()
            result["protocol"] = protocol_var.get()
            result["manual_protocol"] = manual_var.get()
            dialog.destroy()

        def cancel():
            dialog.destroy()

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(fill="x", pady=(4, 4))
        btn_frame.columnconfigure((0, 2, 4), weight=1)
        ttk.Button(btn_frame, text="取消", command=cancel, width=8).grid(row=0, column=1, padx=4)
        ttk.Button(btn_frame, text="确定", command=confirm, width=8).grid(row=0, column=3, padx=4)
        name_entry.bind("<Return>", lambda e: confirm())
        path_entry.bind("<Return>", lambda e: confirm())

        dialog.update_idletasks()
        pw, ph = self.winfo_width(), self.winfo_height()
        px, py = self.winfo_x(), self.winfo_y()
        dw = 460
        dh = dialog.winfo_reqheight()
        x = px + (pw - dw) // 2
        y = py + (ph - dh) // 2
        dialog.geometry(f"{dw}x{dh}+{x}+{y}")
        dialog.grab_set()

        self.wait_window(dialog)
        if not result.get("ok"):
            return

        name = result.get("name", "")
        player_path = result.get("path", path)
        protocol = result.get("protocol", "auto")
        manual_protocol = result.get("manual_protocol", "auto")
        self._players[name] = {"path": player_path, "protocol": protocol, "manual_protocol": manual_protocol}
        _save_config(self._parent._config)
        self._refresh_list()
        self._on_change()

    def _remove(self) -> None:
        sel = self._tree.selection()
        if not sel:
            _show_warning(self, "提示", "请先选择一个播放器")
            return
        name = self._tree.item(sel[0], "values")[0]
        if len(self._players) <= 1:
            _show_warning(self, "提示", "至少保留一个播放器")
            return
        if not _ask_yes_no(self, "确认删除", f"确定要删除播放器 \"{name}\" 吗？"):
            return
        del self._players[name]
        _save_config(self._parent._config)
        self._refresh_list()
        self._on_change()

    def _edit(self) -> None:
        sel = self._tree.selection()
        if not sel:
            _show_warning(self, "提示", "请先选择一个播放器")
            return
        old_name = self._tree.item(sel[0], "values")[0]
        old_entry = self._players.get(old_name, "")
        old_path = _get_player_path(old_entry)
        old_protocol = _get_player_protocol(old_entry)
        old_manual = _get_player_manual_protocol(old_entry)

        dialog = tk.Toplevel(self)
        dialog.title("编辑播放器")
        dialog.minsize(420, 0)
        dialog.resizable(True, True)

        ttk.Label(dialog, text="播放器名称:").pack(anchor="w", padx=10, pady=(6, 2))
        name_var = tk.StringVar(value=old_name)
        name_entry = ttk.Entry(dialog, textvariable=name_var)
        name_entry.pack(fill="x", padx=10, pady=(0, 2))
        name_entry.select_range(0, tk.END)
        name_entry.focus_set()

        ttk.Label(dialog, text="播放器路径:").pack(anchor="w", padx=10, pady=(2, 2))
        row_path = ttk.Frame(dialog)
        row_path.pack(fill="x", padx=10)
        path_var = tk.StringVar(value=old_path)
        path_entry = ttk.Entry(row_path, textvariable=path_var)
        path_entry.pack(side="left", fill="x", expand=True)

        def browse_path():
            p = filedialog.askopenfilename(
                title="选择播放器可执行文件",
                filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")],
                parent=dialog,
            )
            if p:
                path_var.set(p)

        ttk.Button(row_path, text="浏览...", command=browse_path).pack(side="left", padx=(4, 0))

        ttk.Label(dialog, text="自动播放协议:").pack(anchor="w", padx=10, pady=(4, 2))
        protocol_var = tk.StringVar(value=old_protocol)
        protocol_combo = ttk.Combobox(dialog, textvariable=protocol_var,
                                       values=["auto", "direct", "playlist", "ipc"],
                                       state="readonly")
        protocol_combo.pack(fill="x", padx=10, pady=(0, 2))
        protocol_combo.bind("<<ComboboxSelected>>", lambda e: dialog.after(10, dialog.focus_set))

        ttk.Label(dialog, text="手动播放协议:").pack(anchor="w", padx=10, pady=(4, 2))
        manual_var = tk.StringVar(value=old_manual)
        manual_combo = ttk.Combobox(dialog, textvariable=manual_var,
                                     values=["auto", "direct", "playlist", "ipc"],
                                     state="readonly")
        manual_combo.pack(fill="x", padx=10, pady=(0, 2))
        manual_combo.bind("<<ComboboxSelected>>", lambda e: dialog.after(10, dialog.focus_set))

        ttk.Button(dialog, text="协议说明",
                   command=lambda: _show_protocol_help(dialog)
                   ).pack(fill="x", padx=10, pady=(6, 0))

        result = {"ok": False}
        def confirm():
            new_name = name_var.get().strip()
            new_path = path_var.get().strip()
            if not new_name or not new_path:
                _show_warning(dialog, "提示", "名称和路径不能为空")
                return
            result["ok"] = True
            result["name"] = new_name
            result["path"] = new_path
            result["protocol"] = protocol_var.get()
            result["manual_protocol"] = manual_var.get()
            dialog.destroy()

        def cancel():
            dialog.destroy()

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(fill="x", pady=(4, 4))
        btn_frame.columnconfigure((0, 2, 4), weight=1)
        ttk.Button(btn_frame, text="取消", command=cancel, width=8).grid(row=0, column=1, padx=4)
        ttk.Button(btn_frame, text="确定", command=confirm, width=8).grid(row=0, column=3, padx=4)
        name_entry.bind("<Return>", lambda e: confirm())
        path_entry.bind("<Return>", lambda e: confirm())

        dialog.update_idletasks()
        pw, ph = self.winfo_width(), self.winfo_height()
        px, py = self.winfo_x(), self.winfo_y()
        dw = 460
        dh = dialog.winfo_reqheight()
        x = px + (pw - dw) // 2
        y = py + (ph - dh) // 2
        dialog.geometry(f"{dw}x{dh}+{x}+{y}")
        dialog.grab_set()

        self.wait_window(dialog)
        self.grab_set()
        self.focus_set()
        if not result.get("ok"):
            return

        new_name = result["name"]
        new_path = result["path"]
        new_protocol = result.get("protocol", "auto")
        new_manual = result.get("manual_protocol", "auto")
        new_entry = {"path": new_path, "protocol": new_protocol, "manual_protocol": new_manual}
        if new_name == old_name:
            self._players[old_name] = new_entry
        else:
            items = [(new_name, new_entry) if k == old_name else (k, v) for k, v in self._players.items()]
            self._players.clear()
            self._players.update(items)
        _save_config(self._parent._config)
        self._refresh_list()
        self._on_change()


class ToolTip:
    """Hover tooltip for a widget, showing text returned by text_getter."""

    def __init__(self, widget: tk.Widget, text_getter: Callable[[], str]) -> None:
        self._text_getter = text_getter
        self._tw: tk.Toplevel | None = None
        widget.bind("<Enter>", self._on_enter, add="+")
        widget.bind("<Leave>", self._on_leave, add="+")
        widget.bind("<Motion>", self._on_motion, add="+")

    def _on_enter(self, event: tk.Event) -> None:
        self.hide()
        text = self._text_getter()
        if not text:
            return
        self._tw = tk.Toplevel(event.widget)
        self._tw.wm_overrideredirect(True)
        self._tw.wm_geometry(f"+{event.x_root + 12}+{event.y_root + 8}")
        lbl = tk.Message(
            self._tw, text=text, bg="#ffffff", fg="#333",
            font=("Microsoft YaHei", 9), padx=6, pady=2,
            relief="solid", borderwidth=1, width=400,
            aspect=800,
        )
        lbl.pack()

    def _on_leave(self, event: tk.Event) -> None:
        self.hide()

    def _on_motion(self, event: tk.Event) -> None:
        if self._tw:
            self._tw.wm_geometry(f"+{event.x_root + 12}+{event.y_root + 8}")

    def hide(self) -> None:
        if self._tw:
            self._tw.destroy()
            self._tw = None


class TreeviewToolTip:
    """Hover tooltip showing the full text of the treeview cell under cursor.

    The popup window is created once and then only shown/hidden, because
    destroying and re-creating a Toplevel on every <Motion> event made the
    table drop frames while the pointer moved across it.
    """

    def __init__(self, treeview: ttk.Treeview) -> None:
        self._tv = treeview
        self._tw: tk.Toplevel | None = None
        self._lbl: tk.Message | None = None
        self._text = ""
        treeview.bind("<Motion>", self._on_motion, add="+")
        treeview.bind("<Leave>", self._on_leave, add="+")

    def _on_motion(self, event: tk.Event) -> None:
        row = self._tv.identify_row(event.y)
        col = self._tv.identify_column(event.x)
        region = self._tv.identify_region(event.x, event.y)
        if region != "cell" or not row or not col:
            self.hide()
            return
        values = self._tv.item(row, "values")
        try:
            col_idx = int(col[1:]) - 1
        except ValueError:
            self.hide()
            return
        if 0 <= col_idx < len(values) and values[col_idx]:
            self._show(event, str(values[col_idx]))
        else:
            self.hide()

    def _on_leave(self, event: tk.Event) -> None:
        self.hide()

    def _ensure_window(self) -> bool:
        """Create the popup lazily; returns True if it is usable."""
        if self._tw is not None:
            try:
                if self._tw.winfo_exists():
                    return True
            except tk.TclError:
                pass
            self._tw = None
            self._lbl = None
        try:
            self._tw = tk.Toplevel(self._tv)
            self._tw.wm_overrideredirect(True)
            self._lbl = tk.Message(
                self._tw, text="", bg="#ffffff", fg="#333",
                font=("Microsoft YaHei", 9), padx=6, pady=2,
                relief="solid", borderwidth=1, width=400,
                aspect=800,
            )
            self._lbl.pack()
            self._tw.withdraw()
            self._text = ""
            return True
        except tk.TclError:
            self._tw = None
            self._lbl = None
            return False

    def _show(self, event: tk.Event, text: str) -> None:
        if not self._ensure_window():
            return
        if text != self._text:
            self._lbl.configure(text=text)
            self._text = text
        self._tw.wm_geometry(f"+{event.x_root + 12}+{event.y_root + 8}")
        self._tw.deiconify()

    def hide(self) -> None:
        if self._tw is None:
            return
        try:
            self._tw.withdraw()
        except tk.TclError:
            self._tw = None
            self._lbl = None


class ListboxToolTip:
    """Hover tooltip showing the full text of the listbox item under cursor."""

    def __init__(self, listbox: tk.Listbox) -> None:
        self._lb = listbox
        self._tw: tk.Toplevel | None = None
        listbox.bind("<Motion>", self._on_motion, add="+")
        listbox.bind("<Leave>", self._on_leave, add="+")

    def _on_motion(self, event: tk.Event) -> None:
        idx = self._lb.nearest(event.y)
        if 0 <= idx < self._lb.size():
            bbox = self._lb.bbox(idx)
            if bbox and bbox[1] <= event.y <= bbox[1] + bbox[3]:
                text = self._lb.get(idx)
                if text:
                    self._show(event, text)
                    return
        self.hide()

    def _on_leave(self, event: tk.Event) -> None:
        self.hide()

    def _show(self, event: tk.Event, text: str) -> None:
        self.hide()
        self._tw = tk.Toplevel(self._lb)
        self._tw.wm_overrideredirect(True)
        self._tw.wm_geometry(f"+{event.x_root + 12}+{event.y_root + 8}")
        lbl = tk.Message(
            self._tw, text=text, bg="#ffffff", fg="#333",
            font=("Microsoft YaHei", 9), padx=6, pady=2,
            relief="solid", borderwidth=1, width=400,
            aspect=800,
        )
        lbl.pack()

    def hide(self) -> None:
        if self._tw:
            self._tw.destroy()
            self._tw = None


class App(tk.Tk):
    """Main application window."""

    MIN_WIDTH = 400
    MIN_HEIGHT = 300

    def __init__(self) -> None:
        super().__init__()
        self.title("screen-mirroring-capture v1.6.0")
        try:
            if getattr(sys, "frozen", False):
                _base = Path(sys._MEIPASS) / "assets"
            else:
                _base = Path(__file__).resolve().parent.parent / "assets"
            _icon = _base / "icon.ico"
            if _icon.exists():
                self.iconbitmap(str(_icon))
        except Exception:
            pass
        self.minsize(self.MIN_WIDTH, self.MIN_HEIGHT)
        self.geometry("690x610")
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        style = ttk.Style()
        style.configure("TLabelframe.Label", font=("Microsoft YaHei", 11, "bold"))
        style.configure(".", focuscolor="")
        self.configure(bg="#e0e0e0")

        self._running = False
        self._recording = False
        self._closing = False
        self._stop_event = threading.Event()
        self._capture_thread: threading.Thread | None = None
        self._record_proc: subprocess.Popen | None = None
        self._player_proc: subprocess.Popen | None = None
        self._mpv_generation: int = 0
        self._stream_duration: float | None = None
        self._config = _load_config()
        self._history_loaded = False
        self._current_log_file: Path | None = None
        self._continuous_var = tk.BooleanVar(value=False)
        self._capture_count = 0
        self._playlist_path: Path | None = None
        self._playlist_urls: list[dict] = []
        # url -> parsed stream info; filled by ffprobe outside the UI thread.
        self._stream_info_cache: dict[str, dict] = {}
        self._probe_pending: set[str] = set()
        self._last_player_name: str = ""
        self._ipc_pipe: str = ""
        self._last_manual_click_time: float = 0.0
        self._manual_player_procs: list[subprocess.Popen] = []
        self._manual_kill_prev_var = tk.BooleanVar(value=self._config.get("manual_kill_prev", False))
        self._force_direct_var = tk.BooleanVar(value=self._config.get("force_direct", False))
        self._log_tab: str = "current"
        self._ui_queue: queue.Queue[tuple] = queue.Queue()
        self._drain_job: str | None = None
        self._auto_play_last_url: str = ""
        self._auto_play_last_ts: float = 0.0
        self._layout_in_progress: bool = False

        self._build_ui()
        self._remove_focus_ring()
        self._setup_logging()
        self.after(50, self._drain_pending_calls)
        self.bind_all("<Button-1>", self._on_global_click, add="+")

    def _on_global_click(self, event) -> None:
        w = event.widget
        try:
            cls = w.winfo_class()
        except tk.TclError:
            return
        if cls in _BUTTON_CLASSES:
            # ttk hands keyboard focus to the clicked button, and the theme then
            # paints a dotted focus ring around it; give the focus back so the
            # ring disappears as soon as the click is over.
            _drop_focus(w)
            return
        if isinstance(w, (
            ttk.Entry, ttk.Combobox, tk.Text,
            ttk.Spinbox, tk.Listbox, ttk.Scrollbar, ttk.Progressbar,
        )):
            return
        try:
            if w.winfo_toplevel().overrideredirect():
                return
        except Exception:
            return
        w.after(0, w.focus_set)

    def _remove_focus_ring(self) -> None:
        """Remove dotted focus rectangles from all ttk widgets."""
        def _recursive(parent):
            for child in parent.winfo_children():
                if isinstance(child, (ttk.Entry, ttk.Button, ttk.Checkbutton, ttk.Combobox)):
                    try:
                        child.configure(takefocus=0)
                    except Exception:
                        pass
                _recursive(child)
        _recursive(self)

    def _on_close(self) -> None:
        self._closing = True
        # Cancel the repeating UI-queue poll and any pending resize re-flow so
        # no callback fires after the widgets are gone ("invalid command name").
        for job in (self._drain_job, getattr(self._content_width_sync, "_job", None)):
            if job:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        self._drain_job = None
        try:
            faulthandler.cancel_dump_traceback_later()
        except Exception:
            pass
        try:
            self._prune_player_procs()
        except Exception:
            pass
        if self._current_log_file:
            _merge_session_to_history(self._current_log_file)
        self.destroy()

    def _build_ui(self) -> None:
        self._build_scrollable_panel()
        self._build_device_settings()
        self._build_capture_panel()
        self._build_recording_panel()
        self._build_log_panel()

    def _build_scrollable_panel(self) -> None:
        self._paned = tk.PanedWindow(self, orient="vertical", sashwidth=8, sashrelief="ridge", bg="#e0e0e0")
        self._paned.pack(fill="both", expand=True)

        top_frame = ttk.Frame(self._paned)
        self._content_canvas = tk.Canvas(top_frame, highlightthickness=0, bd=0)
        self._content_scrollbar = ttk.Scrollbar(top_frame, orient="vertical", command=self._content_canvas.yview)
        self._content_canvas.configure(yscrollcommand=self._content_scrollbar.set)
        self._content_frame = ttk.Frame(self._content_canvas)
        self._content_window = self._content_canvas.create_window((0, 0), window=self._content_frame, anchor="nw")

        def _on_content_configure(event):
            self._content_canvas.configure(scrollregion=self._content_canvas.bbox("all"))
        self._content_frame.bind("<Configure>", _on_content_configure)

        # Re-flowing the content frame is the expensive part of a resize (it
        # drags every child through the geometry manager), so the width is
        # applied once the drag settles. The first paint is applied
        # immediately to avoid a visible jump on startup.
        self._content_width = 0
        self._content_width_sync = _DebouncedCall(self, _RESIZE_SETTLE_MS,
                                                  self._apply_content_width)

        def _on_canvas_configure(event):
            if self._content_width <= 0:
                self._apply_content_width(event.width)
            else:
                self._content_width_sync(event.width)
        self._content_canvas.bind("<Configure>", _on_canvas_configure)

        def _on_mousewheel(event):
            if self.grab_current() is not None and self.grab_current() != self:
                return
            x, y = self.winfo_pointerxy()
            w = self.winfo_containing(x, y)
            if isinstance(w, (tk.Text, ttk.Spinbox, ttk.Combobox)):
                return
            cx = self._content_canvas.winfo_rootx()
            cy = self._content_canvas.winfo_rooty()
            cw = self._content_canvas.winfo_width()
            ch = self._content_canvas.winfo_height()
            if cx <= x <= cx + cw and cy <= y <= cy + ch:
                self._content_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        self.bind_all("<MouseWheel>", _on_mousewheel)

        self._content_scrollbar.pack(side="right", fill="y")
        self._content_canvas.pack(side="left", fill="both", expand=True)
        self._content_canvas.bind("<Button-1>", lambda e: self.focus_set())
        self._content_frame.bind("<Button-1>", lambda e: self.focus_set())
        self._paned.add(top_frame, stretch="always")

    def _apply_content_width(self, width: int) -> None:
        """Match the scrollable content to the canvas width (coalesced)."""
        if width <= 1 or width == self._content_width:
            return
        self._content_width = width
        try:
            self._content_canvas.itemconfig(self._content_window, width=width)
        except tk.TclError:
            pass

    def _build_device_settings(self) -> None:
        pd = dict(padx=8, pady=4)
        dev_wrapper = tk.Frame(self._content_frame, highlightbackground="#888", highlightthickness=1, bd=0, bg="#e0e0e0")
        dev_wrapper.pack(fill="x", **pd)
        dev_wrapper.bind("<Button-1>", lambda e: self.focus_set())
        frm_dev = ttk.LabelFrame(dev_wrapper, text="设备设置", padding=8)
        frm_dev.pack(fill="x", expand=True)

        row1 = ttk.Frame(frm_dev)
        row1.pack(fill="x")
        row1.columnconfigure(1, weight=1)
        row1.columnconfigure(3, weight=1)
        ttk.Label(row1, text="设备名称:").grid(row=0, column=0, sticky="w")
        self._name_var = tk.StringVar(value=self._config.get("device_name", "screen-mirroring-capture"))
        self._name_entry = ttk.Entry(row1, textvariable=self._name_var)
        self._name_entry.grid(row=0, column=1, sticky="ew", padx=(4, 16))
        self._name_entry.bind("<FocusOut>", lambda e: self._save_device_settings())
        _bind_enter_commit(self._name_entry, self._commit_device_name)

        ttk.Label(row1, text="网络:").grid(row=0, column=2, sticky="w")
        self._bind_ip_var = tk.StringVar(value=self._config.get("bind_ip", ""))
        adapters = list_adapters()
        adapter_options = ["自动检测"]
        self._adapter_map: dict[str, str] = {"自动检测": ""}
        for ip, name in adapters:
            label = f"{ip} ({name})"
            adapter_options.append(label)
            self._adapter_map[label] = ip
        self._net_combo = ttk.Combobox(
            row1, textvariable=self._bind_ip_var, values=adapter_options,
            state="readonly",
        )
        saved_ip = self._config.get("bind_ip", "")
        if saved_ip:
            for label, ip in self._adapter_map.items():
                if ip == saved_ip:
                    self._bind_ip_var.set(label)
                    break
            else:
                self._bind_ip_var.set("自动检测")
        else:
            self._bind_ip_var.set("自动检测")
        self._net_combo.grid(row=0, column=3, sticky="ew", padx=(4, 0))
        self._net_combo.bind("<<ComboboxSelected>>", lambda e: (self._save_device_settings(), self._net_tt.hide(), self.after(10, self.focus_set)))
        self._net_tt = ToolTip(self._net_combo, lambda: self._bind_ip_var.get())
        self._net_combo.bind("<Button-1>", lambda e: self._net_tt.hide(), add="+")
        self._net_combo.bind("<FocusOut>", lambda e: self._net_tt.hide(), add="+")

        row_proto = FlowFrame(frm_dev)
        row_proto.pack(fill="x", pady=(6, 0))
        self._proto_vars: dict[str, tk.BooleanVar] = {}
        self._port_vars: dict[str, tk.StringVar] = {}
        self._proto_checkbuttons: dict[str, ttk.Checkbutton] = {}
        self._port_entries: dict[str, ttk.Entry] = {}
        saved_protos = self._config.get("protocols", {})
        for index, proto in enumerate(PROTOCOLS):
            proto_cfg = saved_protos.get(proto, {})
            enabled = proto_cfg.get("enabled", True)
            # Older configs may hold 0 or garbage; fall back to the default.
            port = _parse_port(proto_cfg.get("port", _DEFAULT_PORTS[proto])) or _DEFAULT_PORTS[proto]
            var = tk.BooleanVar(value=enabled)
            self._proto_vars[proto] = var
            cb = ttk.Checkbutton(row_proto, text=proto.upper(), variable=var, command=self._save_device_settings)
            row_proto.add(cb, padx=(0 if index == 0 else 8, 2))
            self._proto_checkbuttons[proto] = cb
            row_proto.add(ttk.Label(row_proto, text="端口:"), padx=(2, 0))
            port_var = tk.StringVar(value=str(port))
            self._port_vars[proto] = port_var
            pe = ttk.Entry(row_proto, textvariable=port_var, width=6)
            row_proto.add(pe, padx=(2, 6))
            pe.bind("<FocusOut>", lambda e: self._save_device_settings())
            _bind_enter_commit(pe, self._save_device_settings)
            self._port_entries[proto] = pe
        self._restore_btn = ttk.Button(row_proto, text="恢复默认", command=self._restore_defaults)
        row_proto.add(self._restore_btn, padx=(8, 0))

        self._verbose_var = tk.BooleanVar(value=self._config.get("verbose", False))

    def _build_capture_panel(self) -> None:
        pd = dict(padx=8, pady=4)
        capture_wrapper = tk.Frame(self._content_frame, highlightbackground="#888", highlightthickness=1, bd=0, bg="#e0e0e0")
        capture_wrapper.pack(fill="x", **pd)
        capture_wrapper.bind("<Button-1>", lambda e: self.focus_set())
        frm_capture = ttk.LabelFrame(capture_wrapper, text="捕获", padding=8)
        frm_capture.pack(fill="x", expand=True)

        row_ctrl = FlowFrame(frm_capture)
        row_ctrl.pack(fill="x")
        # Order asked for: state | start/stop | continuous | history | playlist.
        # The label gets a fixed width so the growing text ("持续捕获中 (12)")
        # cannot push the buttons around while a capture is running.  17 chars
        # covers the longest string ("● 已停止 (共 999 个)", 113 px).
        self._status_var = tk.StringVar(value="● 已停止")
        self._status_label = ttk.Label(row_ctrl, textvariable=self._status_var,
                                       foreground="red", width=17, anchor="w")
        row_ctrl.add(self._status_label, padx=(0, 8))
        self._run_btn = ttk.Button(row_ctrl, text=_RUN_BTN_TEXT["idle"],
                                   command=self._toggle_capture, width=10)
        row_ctrl.add(self._run_btn, padx=(0, 12))
        self._continuous_cb = ttk.Checkbutton(row_ctrl, text="持续捕获", variable=self._continuous_var)
        row_ctrl.add(self._continuous_cb, padx=(0, 16))
        row_ctrl.add(ttk.Button(row_ctrl, text="📋 捕获历史", command=self._show_history, width=10),
                     padx=(0, 4))
        row_ctrl.add(ttk.Button(row_ctrl, text="▶ 播放列表", command=self._open_playlist_dialog, width=11),
                     padx=(0, 4))
        row_url = ttk.Frame(frm_capture)
        row_url.pack(fill="x", pady=(8, 0))
        self._url_prefix_var = tk.StringVar(value="捕获URL:")
        self._url_prefix_label = tk.Label(row_url, textvariable=self._url_prefix_var, foreground="blue", bg="#e0e0e0")
        self._url_prefix_label.pack(side="left")
        self._url_var = tk.StringVar()
        self._url_entry = ttk.Entry(row_url, textvariable=self._url_var, state="readonly")
        self._url_entry.pack(side="left", fill="x", expand=True, padx=4)
        self._url_tip = ToolTip(self._url_entry, lambda: self._url_var.get())
        ttk.Button(row_url, text="📋 复制", command=self._copy_url).pack(side="left")

        frm_info = ttk.Frame(frm_capture)
        frm_info.pack(fill="x", pady=(4, 0))
        ttk.Label(frm_info, text="流信息:").pack(side="left")
        self._info_var = tk.StringVar(value="")
        ttk.Label(frm_info, textvariable=self._info_var, foreground="blue").pack(side="left", padx=(4, 0))

        row_remark = ttk.Frame(frm_capture)
        row_remark.pack(fill="x", pady=(4, 0))
        ttk.Label(row_remark, text="备注:").pack(side="left")
        self._remark_var = tk.StringVar(value="")
        self._remark_frame = ttk.Frame(row_remark)
        self._remark_frame.pack(side="left", padx=(4, 0))
        self._remark_btn = ttk.Button(row_remark, text="", command=self._edit_remark_dialog, width=10)
        self._remark_btn.pack(side="left", padx=(4, 0))
        self._update_remark_mode()

        self._player_buttons_frame = FlowFrame(frm_capture)
        self._player_buttons_frame.pack(fill="x", pady=(8, 0))
        self._player_buttons: dict[str, ttk.Button] = {}

        self._layout_player_buttons()
        self._player_buttons_frame.bind("<Button-3>", self._on_player_buttons_right_click)

        row_auto = FlowFrame(frm_capture)
        row_auto.pack(fill="x", pady=(4, 0))
        self._auto_play_var = tk.BooleanVar(value=self._config.get("auto_play", False))
        row_auto.add(ttk.Checkbutton(row_auto, text="自动播放:", variable=self._auto_play_var,
                                     command=self._save_auto_play_settings))
        player_names = list(self._config.get("players", {}).keys())
        self._auto_play_player_var = tk.StringVar(value=self._config.get("auto_play_player", ""))
        self._auto_play_combo = ttk.Combobox(row_auto, textvariable=self._auto_play_player_var,
                                              values=player_names, width=20, state="readonly")
        row_auto.add(self._auto_play_combo, padx=(4, 0))
        self._auto_play_combo.bind("<<ComboboxSelected>>", lambda e: self.after(10, self.focus_set))
        row_auto.add(ttk.Checkbutton(row_auto, text="杀旧进程", variable=self._manual_kill_prev_var,
                                     command=self._save_manual_kill_settings), padx=(12, 0))
        row_auto.add(ttk.Checkbutton(row_auto, text="全局直连", variable=self._force_direct_var,
                                     command=self._save_force_direct_settings), padx=(12, 0))
        row_auto.add(ttk.Label(row_auto, text="防抖(秒):"), padx=(12, 2))
        self._debounce_var = tk.DoubleVar(value=self._config.get("manual_click_debounce", 2.0))
        debounce_spin = ttk.Spinbox(row_auto, from_=0.5, to=10.0, increment=0.5,
                                     textvariable=self._debounce_var, width=5)
        row_auto.add(debounce_spin, padx=(0, 4))
        def _debounce_wheel(event):
            self._content_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            return "break"
        debounce_spin.bind("<MouseWheel>", _debounce_wheel)
        debounce_spin.bind("<Button-4>", _debounce_wheel)
        debounce_spin.bind("<Button-5>", _debounce_wheel)
        debounce_spin.bind("<FocusOut>", lambda e: self._save_debounce_settings())
        _bind_enter_commit(debounce_spin, self._save_debounce_settings)

    def _build_recording_panel(self) -> None:
        pd = dict(padx=8, pady=4)
        rec_wrapper = tk.Frame(self._content_frame, highlightbackground="#888", highlightthickness=1, bd=0, bg="#e0e0e0")
        rec_wrapper.pack(fill="x", **pd)
        rec_wrapper.bind("<Button-1>", lambda e: self.focus_set())
        frm_rec = ttk.LabelFrame(rec_wrapper, text="录制", padding=8)
        frm_rec.pack(fill="x", expand=True)

        row_rec = ttk.Frame(frm_rec)
        row_rec.pack(fill="x")
        ttk.Label(row_rec, text="默认保存目录:").pack(side="left")
        self._save_dir_var = tk.StringVar(value=self._config.get("save_dir", ""))
        self._save_dir_entry = ttk.Entry(row_rec, textvariable=self._save_dir_var, width=40)
        self._save_dir_entry.pack(side="left", padx=(4, 4), fill="x", expand=True)
        _bind_enter_commit(self._save_dir_entry, self._commit_save_dir)
        ttk.Button(row_rec, text="浏览", command=self._browse_dir).pack(side="left")

        row_audio = ttk.Frame(frm_rec)
        row_audio.pack(fill="x", pady=(6, 0))
        self._audio_rec_var = tk.BooleanVar(value=bool(self._config.get("audio_record", False)))
        ttk.Checkbutton(row_audio, text="录制 AirPlay 音频", variable=self._audio_rec_var,
                        command=self._save_audio_output).pack(side="left")
        self._audio_output_var = tk.StringVar(value=self._config.get("audio_output", ""))
        self._audio_output_entry = ttk.Entry(row_audio, textvariable=self._audio_output_var, width=30)
        self._audio_output_entry.pack(side="left", padx=(8, 0), fill="x", expand=True)
        _bind_enter_commit(self._audio_output_entry, self._commit_audio_output)
        ttk.Button(row_audio, text="浏览", command=self._browse_audio_dir).pack(side="left")

        self._progress_var = tk.StringVar(value="")
        self._progress_pct_var = tk.StringVar(value="")
        row_progress_title = ttk.Frame(frm_rec)
        row_progress_title.pack(fill="x", pady=(4, 0))
        ttk.Label(row_progress_title, text="录制进度:").pack(side="left")
        ttk.Label(row_progress_title, textvariable=self._progress_var, foreground="green").pack(side="left", padx=(8, 0))
        ttk.Label(row_progress_title, textvariable=self._progress_pct_var, foreground="green").pack(side="right")
        self._progress_bar = ttk.Progressbar(frm_rec, mode="determinate")
        self._progress_bar.pack(fill="x", pady=(2, 0))

        row_rec_ctrl = ttk.Frame(frm_rec)
        row_rec_ctrl.pack(fill="x", pady=(6, 0))
        self._rec_btn = ttk.Button(row_rec_ctrl, text="录制", command=self._on_record_click, width=8, state="disabled")
        self._rec_btn.pack(side="left", padx=4)
        self._stop_rec_btn = ttk.Button(row_rec_ctrl, text="停止录制", command=self._stop_recording, width=10, state="disabled")
        self._stop_rec_btn.pack(side="left", padx=4)
        self._rec_status_var = tk.StringVar(value="")
        ttk.Label(row_rec_ctrl, textvariable=self._rec_status_var, foreground="purple").pack(side="left", padx=16)

    def _build_log_panel(self) -> None:
        pd = dict(padx=8, pady=4)
        log_inner = ttk.Frame(self._paned)
        log_inner.pack_propagate(False)
        log_inner.configure(height=135)
        # stretch="always" lets the log share the extra height when the window
        # is enlarged; with "never" it stayed at 135 px and the log area never
        # adapted. Dragging the sash still sets a manual split.
        self._paned.add(log_inner, minsize=70, stretch="always")
        tk.Frame(log_inner, height=3, bg="#888").pack(fill="x", pady=(0, 4))

        row_bottom = ttk.Frame(log_inner)
        row_bottom.pack(side="bottom", fill="x", **pd)
        self._log_tab_var = tk.StringVar(value="current")
        self._log_current_rb = ttk.Radiobutton(row_bottom, text="当前日志",
                                                variable=self._log_tab_var, value="current",
                                                command=self._switch_log_tab, takefocus=0)
        self._log_current_rb.pack(side="left", padx=2)
        self._log_history_rb = ttk.Radiobutton(row_bottom, text="历史日志",
                                                variable=self._log_tab_var, value="history",
                                                command=self._switch_log_tab, takefocus=0)
        self._log_history_rb.pack(side="left", padx=2)
        self._verbose_cb = ttk.Checkbutton(row_bottom, text="启用调试日志",
                                            variable=self._verbose_var, command=self._on_verbose_change)
        self._verbose_cb.pack(side="left", padx=(12, 2))
        self._clear_btn = ttk.Button(row_bottom, text="清除当前日志", command=self._clear_log)
        self._clear_btn.pack(side="right", padx=2)

        log_wrapper = tk.Frame(log_inner, highlightbackground="#888", highlightthickness=1, bd=0, bg="#e0e0e0")
        log_wrapper.pack(fill="both", expand=True, **pd)
        frm_log = ttk.LabelFrame(log_wrapper, text="日志", padding=8)
        frm_log.pack(fill="both", expand=True)

        self._log_container = ttk.Frame(frm_log)
        self._log_container.pack(fill="both", expand=True)

        self._current_log_frame = ttk.Frame(self._log_container)
        self._log_text = tk.Text(self._current_log_frame, height=8, wrap="word",
                                 state="normal", relief="sunken", bd=1,
                                 bg="#fafafa",
                                 selectbackground="#3399FF", selectforeground="white")
        self._log_text.pack(fill="both", expand=True)
        self._log_text.tag_configure("sel", background="#3399FF", foreground="white")
        self._log_text.tag_raise("sel")
        self._log_text.bind("<Key>", self._block_log_edit_keys)
        self._add_log_copy_menu(self._log_text)

        self._history_log_frame = ttk.Frame(self._log_container)
        self._history_log_text = tk.Text(self._history_log_frame, height=8, wrap="word",
                                          state="normal", relief="sunken", bd=1,
                                          bg="#fafafa",
                                          selectbackground="#3399FF", selectforeground="white")
        self._history_log_text.pack(fill="both", expand=True)
        self._history_log_text.tag_configure("sel", background="#3399FF", foreground="white")
        self._history_log_text.tag_raise("sel")
        self._history_log_text.bind("<Key>", self._block_log_edit_keys)
        self._add_log_copy_menu(self._history_log_text)

        self._log_tab = "current"
        self._current_log_frame.pack(fill="both", expand=True)

    def _browse_dir(self) -> None:
        path = filedialog.askdirectory(title="选择保存目录", parent=self)
        if path:
            self._save_dir_var.set(path)
            self._save_dir_changed()

    def _save_dir_changed(self) -> None:
        self._config["save_dir"] = self._save_dir_var.get().strip()
        _save_config(self._config)

    def _commit_save_dir(self) -> None:
        """Trim the path shown in the entry, then persist it."""
        self._save_dir_var.set(self._save_dir_var.get().strip())
        self._save_dir_changed()

    def _browse_audio_dir(self) -> None:
        path = filedialog.askdirectory(title="选择音频保存目录", parent=self)
        if path:
            self._audio_output_var.set(path)
            self._save_audio_output()

    def _save_audio_output(self) -> None:
        self._config["audio_output"] = self._audio_output_var.get().strip()
        self._config["audio_record"] = self._audio_rec_var.get()
        _save_config(self._config)

    def _commit_audio_output(self) -> None:
        """Trim the path shown in the entry, then persist it."""
        self._audio_output_var.set(self._audio_output_var.get().strip())
        self._save_audio_output()

    def _commit_device_name(self) -> None:
        """Trim the name and keep the entry showing what was actually stored."""
        name = self._name_var.get().strip() or "screen-mirroring-capture"
        self._name_var.set(name)
        self._save_device_settings()

    def _restore_defaults(self) -> None:
        self._name_var.set("screen-mirroring-capture")
        self._bind_ip_var.set("自动检测")
        for proto in PROTOCOLS:
            self._proto_vars[proto].set(True)
            self._port_vars[proto].set(str(_DEFAULT_PORTS[proto]))
        self._save_device_settings()

    def _save_device_settings(self) -> None:
        self._config["device_name"] = self._name_var.get().strip() or "screen-mirroring-capture"
        selected_label = self._bind_ip_var.get()
        self._config["bind_ip"] = self._adapter_map.get(selected_label, "")
        saved_protos = {}
        for proto in PROTOCOLS:
            default = _DEFAULT_PORTS[proto]
            port = _parse_port(self._port_vars[proto].get())
            if port is None:
                # Keep the stored value honest and show the user the real value
                # instead of letting an unparseable entry reach capture().
                port = default
                self._port_vars[proto].set(str(default))
            saved_protos[proto] = {
                "enabled": self._proto_vars[proto].get(),
                "port": port,
            }
        self._config["protocols"] = saved_protos
        _save_config(self._config)

    def _on_verbose_change(self) -> None:
        self._config["verbose"] = self._verbose_var.get()
        _save_config(self._config)
        level = logging.DEBUG if self._verbose_var.get() else logging.INFO
        logging.getLogger().setLevel(level)

    def _setup_logging(self) -> None:
        level = logging.DEBUG if self._config.get("verbose") else logging.INFO
        logging.getLogger().setLevel(level)
        self._text_handler = PersistentTextHandler(self._log_text, self)
        self._text_handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s.%(funcName)s] %(message)s", datefmt="%H:%M:%S")
        self._text_handler.setFormatter(formatter)
        logging.getLogger().addHandler(self._text_handler)
        self._current_log_file = self._text_handler.log_file

    def _show_history(self) -> None:
        HistoryDialog(self)

    def _start(self) -> None:
        # ── Validate everything up front ──────────────────────────────
        # Nothing here may touch the running state: an invalid entry must
        # leave the UI exactly as it was, not half-started.
        protos = [p for p in PROTOCOLS if self._proto_vars[p].get()]
        if not protos:
            _show_warning(self, "提示", "请至少启用一个协议（DLNA / AirPlay / Cast）")
            return

        ports: dict[str, int] = {}
        for proto in PROTOCOLS:
            port = _parse_port(self._port_vars[proto].get())
            if port is None:
                _show_warning(
                    self, "提示",
                    f"{proto.upper()} 端口无效，请填写 1-65535 之间的整数",
                )
                self._port_entries[proto].focus_set()
                return
            ports[proto] = port

        used: dict[int, str] = {}
        for proto in protos:
            other = used.get(ports[proto])
            if other is not None:
                _show_warning(
                    self, "提示",
                    f"{other.upper()} 与 {proto.upper()} 端口冲突（都是 {ports[proto]}），请修改",
                )
                return
            used[ports[proto]] = proto

        audio_output = None
        if self._audio_rec_var.get():
            if "airplay" not in protos:
                _show_warning(self, "提示", "录制 AirPlay 音频需要启用 AirPlay 协议")
                return
            folder = self._audio_output_var.get().strip()
            if not folder:
                _show_warning(self, "提示", "请选择 AirPlay 音频保存目录")
                return
            try:
                Path(folder).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                _show_warning(self, "提示", f"无法创建音频保存目录：{exc}")
                return
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            audio_output = str(Path(folder) / f"airplay_audio_{stamp}.aac")

        # ── Commit ────────────────────────────────────────────────────
        self._save_device_settings()
        self._stop_event.clear()
        self._capture_count = 0
        self._set_running(True)
        bind_ip = self._adapter_map.get(self._bind_ip_var.get(), "")
        if not bind_ip:
            resolved_ip = get_lan_ip()
            for label, ip in self._adapter_map.items():
                if ip == resolved_ip:
                    self._bind_ip_var.set(label)
                    bind_ip = resolved_ip
                    break
        threading.Thread(
            target=self._capture_thread_fn,
            args=(self._config["device_name"], ports, protos, bind_ip, audio_output),
            # Read the Tk variable on this (UI) thread: the worker must not touch Tk.
            kwargs={"continuous": self._continuous_var.get()},
            daemon=True,
        ).start()

    def _stop(self) -> None:
        # Calling this twice is harmless (the event is just re-set): the button
        # stays live while "停止中" so a stuck shutdown never locks the UI.
        self._stop_event.set()
        self._set_run_state("stopping")
        self._show_status("● 正在停止...", "orange")

    def _toggle_capture(self) -> None:
        """Single button: start when idle, stop when running."""
        if self._running:
            self._stop()
        else:
            self._start()

    def _set_run_state(self, mode: str) -> None:
        """Update the merged start/stop button (idle / running / stopping)."""
        if not hasattr(self, "_run_btn"):
            return
        text = _RUN_BTN_TEXT.get(mode, _RUN_BTN_TEXT["idle"])
        try:
            self._run_btn.configure(text=text)
        except tk.TclError:
            pass

    def _open_in_player(self, player_name: str, url: str | None = None) -> None:
        """Open captured URL in a specific player by name."""
        log.debug(">>> _open_in_player: player=%s url_in=%s", player_name, url[:60] if url else None)
        now = time.monotonic()
        debounce = self._config.get("manual_click_debounce", 2.0)
        elapsed = now - self._last_manual_click_time
        log.debug("    debounce: elapsed=%.3fs threshold=%.3fs", elapsed, debounce)
        if elapsed < debounce:
            log.debug("    <<< debounce blocked")
            return
        self._last_manual_click_time = now
        url = url or self._url_var.get()
        if not url and self._playlist_urls:
            url = self._playlist_urls[0]["url"]
        log.debug("    resolved url=%s playlist_len=%d", url[:60] if url else None, len(self._playlist_urls))
        if not url:
            log.debug("    <<< no URL, returning")
            return
        players = self._config.get("players", {})
        entry = players.get(player_name)
        if not entry:
            log.debug("    <<< player '%s' not configured", player_name)
            _show_warning(self, "提示", f"播放器 {player_name} 未配置")
            return
        path_or_cmd = _get_player_path(entry)
        player_path = _find_player_executable(path_or_cmd)
        log.debug("    _find_player_executable(%s) → %s", path_or_cmd, player_path)
        if not player_path:
            log.debug("    <<< player not found, showing warning")
            _show_warning(self, "提示", f"未找到 {player_name} ({path_or_cmd})，请检查路径或重新添加")
            return
        log.debug("    calling _launch_manual_player ...")
        result = self._launch_manual_player(player_name, url)
        log.debug("    _launch_manual_player returned %s", result)
        if not result:
            _show_error(self, "错误", f"启动播放器 {player_name} 失败")

    def _launch_manual_player(self, player_name: str, url: str) -> bool:
        log.debug(">>> _launch_manual_player: player=%s url=%s", player_name, url[:60] if url else None)
        players = self._config.get("players", {})
        entry = players.get(player_name)
        if not entry:
            log.debug("    <<< entry not found")
            return False
        path_or_cmd = _get_player_path(entry)
        protocol = _get_player_manual_protocol(entry)
        if self._force_direct_var.get():
            protocol = "direct"
        player_path = _find_player_executable(path_or_cmd)
        log.debug("    path_or_cmd=%s  protocol=%s  player_path=%s", path_or_cmd, protocol, player_path)
        if not player_path:
            log.debug("    <<< player_path is None, returning False")
            return False

        stem = Path(player_path).stem.lower()

        if protocol == "auto":
            is_playlist = self._is_playlist_stem(stem)
            log.debug("    协议决策: auto → %s (stem=%s)", "playlist" if is_playlist else "direct", stem)
        elif protocol in ("playlist", "ipc"):
            is_playlist = True
            log.debug("    协议决策: %s → playlist", protocol)
        else:
            is_playlist = False
            log.debug("    协议决策: %s → direct", protocol)

        is_duplicate = self._config.get("auto_dedupe", False) and any(i["url"] == url for i in self._playlist_urls)
        if is_duplicate:
            log.debug("    列表去重：URL 已存在，不重复添加")
        else:
            self._append_to_playlist(url)

        # IPC 分支：复用已有 mpv 进程，追加不中断
        if is_playlist and "mpv" in stem and self._ipc_pipe and self._player_proc and self._player_proc.poll() is None:
            log.debug("    IPC 追加: pipe=%s url=%s", self._ipc_pipe, url[:60])
            self._write_playlist()
            if self._mpv_ipc_append(url, _get_ipc_mode(self._config)):
                log.debug("    IPC append 成功")
                return True
            log.debug("    IPC 追加失败，回退到新进程")

        # non-mpv 非 IPC 模式：manual_kill_prev 控制是否杀旧进程
        if not (is_playlist and "mpv" in stem):
            if self._manual_kill_prev_var.get():
                log.debug("    终止旧手动播放器进程 (kill_prev=True)")
                self._stop_player()
                for proc in self._manual_player_procs:
                    if proc.poll() is None:
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                self._manual_player_procs.clear()

        # mpv 非 IPC 模式：终止旧 mpv 进程再启动新的
        if is_playlist and "mpv" in stem and self._player_proc and self._player_proc.poll() is None:
            log.debug("    终止旧 mpv 进程")
            self._stop_player()

        try:
            if is_playlist:
                self._write_playlist()
                if "mpv" in stem:
                    if not self._ipc_pipe or not (self._player_proc and self._player_proc.poll() is None):
                        pipe_name = f"smc-mpv-{os.getpid()}"
                        self._ipc_pipe = pipe_name
                    cmd = [player_path, fr"--input-ipc-server=\\.\pipe\{self._ipc_pipe}", "--idle=yes", "--keep-open=yes", str(self._playlist_path)]
                    log.debug("    Popen(mpv playlist+IPC): %s", cmd)
                    self._player_proc = self._spawn_mpv(cmd, f"手动 {player_name}")
                else:
                    cmd = [player_path, str(self._playlist_path)]
                    if "ffplay" in stem:
                        cmd.insert(1, "-nostats")
                    log.debug("    Popen(playlist): %s", cmd)
                    proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)
                    _log_stderr(proc, f"[手动 {player_name}] ")
                    self._track_player_proc(proc)
            else:
                cmd = [player_path, url]
                if "ffplay" in stem:
                    cmd.insert(1, "-nostats")
                log.debug("    Popen(direct): %s", cmd)
                proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)
                _log_stderr(proc, f"[手动 {player_name}] ")
                self._track_player_proc(proc)

            log.debug("    <<< 成功, 返回 True")
            log.info("手动播放 %s: %s", player_name, url)
            return True
        except Exception as exc:
            log.debug("    <<< 异常: %s", exc)
            log.error("手动播放失败: %s", exc)
            return False

    def _play_single_url(self, player_name: str, url: str) -> bool:
        """Play a single URL without adding to playlist or affecting the queue."""
        log.debug(">>> _play_single_url: player=%s url=%s", player_name, url[:60] if url else None)
        players = self._config.get("players", {})
        entry = players.get(player_name)
        if not entry:
            log.debug("    <<< entry not found")
            return False

        path_or_cmd = _get_player_path(entry)
        player_path = _find_player_executable(path_or_cmd)
        if not player_path:
            log.debug("    <<< player not found")
            _show_warning(self, "提示", f"未找到 {player_name} ({path_or_cmd})，请检查路径")
            return False

        log.debug("    player_path=%s", player_path)

        try:
            cmd = [player_path, url]
            if "ffplay" in Path(player_path).stem.lower():
                cmd.insert(1, "-nostats")
            log.debug("    Popen: %s", cmd)
            proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)
            _log_stderr(proc, f"[单次 {player_name}] ")
            self._track_player_proc(proc)
            log.info("单次播放 %s: %s", player_name, url)
            return True
        except Exception as exc:
            log.debug("    <<< 异常: %s", exc)
            log.error("单次播放失败: %s", exc)
            return False

    def _launch_auto_player(self, player_name: str, url: str) -> bool:
        log.debug(">>> _launch_auto_player: player=%s url=%s", player_name, url[:60] if url else None)
        players = self._config.get("players", {})
        entry = players.get(player_name)
        if not entry:
            log.debug("    <<< entry not found")
            return False
        path_or_cmd = _get_player_path(entry)
        protocol = _get_player_protocol(entry)
        if self._force_direct_var.get():
            protocol = "direct"
        player_path = _find_player_executable(path_or_cmd)
        log.debug("    path_or_cmd=%s  protocol=%s  player_path=%s", path_or_cmd, protocol, player_path)
        if not player_path:
            log.debug("    <<< player_path is None")
            return False

        stem = Path(player_path).stem.lower()

        if protocol == "auto":
            is_playlist = self._is_playlist_stem(stem)
            log.debug("    协议决策: auto → %s (stem=%s)", "playlist" if is_playlist else "direct", stem)
        elif protocol in ("playlist", "ipc"):
            is_playlist = True
            log.debug("    协议决策: %s → playlist", protocol)
        else:
            is_playlist = False
            log.debug("    协议决策: %s → direct", protocol)

        if self._config.get("auto_dedupe", False) and any(i["url"] == url for i in self._playlist_urls):
            log.debug("    <<< 自动去重跳过重复 URL: %s", url[:60])
            return True

        self._append_to_playlist(url)
        log.debug("    队列长度=%d", len(self._playlist_urls))

        # IPC 分支：复用已有 mpv 进程，追加不中断
        if is_playlist and "mpv" in stem and self._ipc_pipe and self._player_proc and self._player_proc.poll() is None:
            log.debug("    IPC 追加尝试: pipe=%s gen=%d url=%s", self._ipc_pipe, getattr(self, "_mpv_generation", 0), url[:60])
            self._write_playlist()
            if self._mpv_ipc_append(url, _get_ipc_mode(self._config)):
                log.debug("    <<< IPC append 成功")
                return True
            log.debug("    IPC 追加失败，将重启进程")

        # 终止旧进程
        if self._player_proc and self._player_proc.poll() is None:
            log.debug("    终止旧 mpv (pid=%s)", self._player_proc.pid)
            self._stop_player()

        self._last_player_name = player_name

        try:
            if is_playlist:
                self._write_playlist()
                if "mpv" in stem:
                    pipe_name = f"smc-mpv-{os.getpid()}"
                    self._ipc_pipe = pipe_name
                    cmd = [player_path, fr"--input-ipc-server=\\.\pipe\{pipe_name}", "--idle=yes", "--keep-open=yes", str(self._playlist_path)]
                    log.debug("    Popen(mpv playlist+IPC): %s", cmd)
                    self._player_proc = self._spawn_mpv(cmd, f"自动 {player_name}")
                else:
                    cmd = [player_path, str(self._playlist_path)]
                    if "ffplay" in stem:
                        cmd.insert(1, "-nostats")
                    log.debug("    Popen(playlist): %s", cmd)
                    self._player_proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)
                    _log_stderr(self._player_proc, f"[自动 {player_name}] ")
            else:
                cmd = [player_path, url]
                if "ffplay" in stem:
                    cmd.insert(1, "-nostats")
                log.debug("    Popen(direct): %s", cmd)
                self._player_proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)
                _log_stderr(self._player_proc, f"[自动 {player_name}] ")

            log.debug("    <<< 成功, 返回 True")
            log.info("自动播放 %s: %s", player_name, url)
            return True
        except Exception as exc:
            log.debug("    <<< 异常: %s", exc)
            log.error("自动播放失败: %s", exc)
            return False

    @staticmethod
    def _is_playlist_stem(stem: str) -> bool:
        return any(x in stem for x in ("vlc", "mpc-hc", "mpc-be", "mpv"))

    def _append_to_playlist(self, url: str, remark: str = "") -> None:
        """Append *url* to the queue without blocking the UI thread.

        Metadata already known for the URL is reused immediately; anything
        missing is probed in the background and patched in afterwards.
        """
        self._playlist_urls.append(
            _make_playlist_item(url, self._stream_info_cache.get(url), remark)
        )
        self._write_playlist()
        self._update_player_button_states()
        if url not in self._stream_info_cache:
            self._probe_async(url)

    def _probe_async(self, url: str) -> None:
        """Probe *url* on a worker thread; never blocks Tk."""
        if not url or url in self._stream_info_cache or url in self._probe_pending:
            return
        self._probe_pending.add(url)
        threading.Thread(
            target=self._probe_worker, args=(url,), daemon=True
        ).start()

    def _probe_worker(self, url: str) -> None:
        try:
            data = _get_stream_info(url)
            info = _parse_stream_info(data) if data and not data.get("error") else {}
        except Exception:
            log.debug("probe worker failed for %s", url, exc_info=True)
            info = {}
        self._call_ui(self._apply_probe_result, url, info)

    def _apply_probe_result(self, url: str, info: dict) -> None:
        """Cache probe output and back-fill queue entries waiting for it."""
        self._probe_pending.discard(url)
        self._stream_info_cache[url] = info
        changed = False
        for item in self._playlist_urls:
            if item["url"] != url:
                continue
            if any(item.get(k) for k in _META_FIELDS):
                continue
            for key in _META_FIELDS:
                if info.get(key):
                    item[key] = info[key]
            changed = True
        if changed:
            self._write_playlist()

    def _write_playlist(self) -> None:
        if self._playlist_path is None:
            self._playlist_path = Path(tempfile.gettempdir()) / f"smc_playlist_{os.getpid()}.m3u"
        lines = ["#EXTM3U\n"]
        for item in self._playlist_urls:
            lines.append(f"{item['url']}\n")
        self._playlist_path.write_text("".join(lines), encoding="utf-8")

    def _mpv_ipc_append(self, url: str, mode: str = "append-play") -> bool:
        if not self._ipc_pipe:
            log.debug("IPC append 失败: _ipc_pipe 为空")
            return False
        pipe_path = f"\\\\.\\pipe\\{self._ipc_pipe}"
        try:
            import win32file
            import win32pipe
            import pywintypes
            try:
                handle = win32file.CreateFile(
                    pipe_path,
                    win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    0, None, win32file.OPEN_EXISTING, 0, None
                )
            except pywintypes.error as e:
                log.warning("IPC CreateFile 失败 pipe=%s: errno=%s msg=%s", self._ipc_pipe, e.args[0], e.args[1])
                return False
            data = json.dumps({"command": ["loadfile", url, mode]}) + "\n"
            try:
                win32file.WriteFile(handle, data.encode("utf-8"))
            except pywintypes.error as e:
                log.warning("IPC WriteFile 失败 pipe=%s url=%s: errno=%s msg=%s", self._ipc_pipe, url, e.args[0], e.args[1])
                win32file.CloseHandle(handle)
                return False
            # Read response to prevent pipe buffer from blocking mpv
            try:
                mode = win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_NOWAIT
                win32pipe.SetNamedPipeHandleState(handle, mode, None, None)
                _, response = win32file.ReadFile(handle, 4096)
                log.debug("mpv IPC响应: %s", response.decode("utf-8", errors="replace").strip())
            except Exception:
                pass
            win32file.CloseHandle(handle)
            log.debug("IPC append 成功: pipe=%s url=%s", self._ipc_pipe, url)
            return True
        except Exception as e:
            log.warning("IPC append 异常: pipe=%s url=%s: %s", self._ipc_pipe, url, e)
            return False

    def _prune_player_procs(self) -> None:
        """Drop references to manual player processes that already exited.

        Without this the list only ever grew, leaking one Popen object per
        manual playback for the lifetime of the app.
        """
        if not self._manual_player_procs:
            return
        alive: list[subprocess.Popen] = []
        for proc in self._manual_player_procs:
            try:
                if proc.poll() is None:
                    alive.append(proc)
            except Exception:
                continue
        if len(alive) != len(self._manual_player_procs):
            self._manual_player_procs[:] = alive

    def _track_player_proc(self, proc: subprocess.Popen) -> None:
        """Remember a manual player process, pruning the ones already gone."""
        self._prune_player_procs()
        self._manual_player_procs.append(proc)

    def _stop_player(self) -> None:
        if self._player_proc and self._player_proc.poll() is None:
            try:
                self._player_proc.terminate()
                self._player_proc.wait(timeout=1)
            except Exception:
                try:
                    self._player_proc.kill()
                    self._player_proc.wait(timeout=1)
                except Exception:
                    pass
            log.info("自动播放进程已终止")
        self._player_proc = None
        self._mpv_generation = getattr(self, "_mpv_generation", 0) + 1

    def _reset_mpv_state(self) -> None:
        import traceback
        log.warning("=== _reset_mpv_state 被调用 === 调用栈:\n%s", "".join(traceback.format_stack()))
        self._playlist_urls.clear()
        self._ipc_pipe = ""
        self._playlist_path = None
        self._player_proc = None
        self._manual_player_procs.clear()
        log.info("mpv 共享状态已重置")

    def _clear_queue(self) -> None:
        if self._player_proc and self._player_proc.poll() is None:
            try:
                self._player_proc.terminate()
                self._player_proc.wait(timeout=1)
            except Exception:
                try:
                    self._player_proc.kill()
                except Exception:
                    pass
        for proc in self._manual_player_procs:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        self._reset_mpv_state()
        _show_toast(self, "播放队列已清空")

    def _spawn_mpv(self, cmd: list[str], name: str) -> subprocess.Popen:
        self._mpv_generation = getattr(self, "_mpv_generation", 0) + 1
        gen = self._mpv_generation
        proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)
        _log_stderr(proc, f"[{name} gen={gen}] ")

        def _watch():
            proc.wait()
            log.info("mpv gen=%d 进程退出 (pid=%d)，调度主线程检查", gen, proc.pid)
            self.after(0, lambda: self._on_mpv_exit(gen))

        threading.Thread(target=_watch, daemon=True).start()
        return proc

    def _on_mpv_exit(self, gen: int) -> None:
        current = getattr(self, "_mpv_generation", 0)
        if current == gen:
            log.warning("mpv gen=%d 退出 (pid=%d), 队列长度=%d, 保留队列仅清管道引用",
                        gen, getattr(self, "_player_proc", None) and self._player_proc.pid or 0,
                        len(self._playlist_urls))
            self._player_proc = None
            self._ipc_pipe = ""
        else:
            log.debug("mpv 旧进程 gen=%d 退出，忽略（当前 gen=%d）", gen, current)

    def _save_auto_play_settings(self) -> None:
        self._config["auto_play"] = self._auto_play_var.get()
        self._config["auto_play_player"] = self._auto_play_player_var.get()
        _save_config(self._config)

    def _save_debounce_settings(self) -> None:
        try:
            val = self._debounce_var.get()
        except (tk.TclError, ValueError):
            # Unparseable text (e.g. the box was cleared): fall back to the
            # value that is actually in effect instead of leaving it on screen.
            val = self._config.get("manual_click_debounce", 2.0)
        val = max(0.5, min(10.0, val))
        self._debounce_var.set(val)
        self._config["manual_click_debounce"] = val
        _save_config(self._config)

    def _open_playlist_dialog(self) -> None:
        PlaylistDialog(self)

    def _save_manual_kill_settings(self) -> None:
        self._config["manual_kill_prev"] = self._manual_kill_prev_var.get()
        _save_config(self._config)

    def _save_force_direct_settings(self) -> None:
        self._config["force_direct"] = self._force_direct_var.get()
        _save_config(self._config)

    def _layout_player_buttons(self) -> None:
        """Create or re-flow the player buttons for the current width.

        Widgets are only (re)created when the player list actually changed;
        a plain resize just repositions the existing ones inside the FlowFrame.
        """
        if self._layout_in_progress:
            return
        self._layout_in_progress = True
        try:
            players = list(self._config.get("players", {}))
            if list(self._player_buttons) != players:
                self._create_player_buttons(players)
            self._player_buttons_frame.relayout()
        finally:
            self._layout_in_progress = False

    def _create_player_buttons(self, players: list[str]) -> None:
        frame = self._player_buttons_frame
        frame.clear()
        self._player_buttons.clear()
        frame.add(ttk.Label(frame, text="播放器:"))
        for name in players:
            btn = ttk.Button(frame, text=f"▶ {name}",
                             command=lambda n=name: self._open_in_player(n))
            frame.add(btn, padx=(4, 0))
            self._player_buttons[name] = btn
        self._edit_btn = ttk.Button(frame, text="编辑", command=self._open_player_dialog)
        frame.add(self._edit_btn, padx=(4, 0))
        self._update_player_button_states()

    def _on_player_buttons_right_click(self, event: tk.Event) -> None:
        pass

    def _rebuild_player_buttons(self) -> None:
        """Rebuild the player buttons in the capture card."""
        if not hasattr(self, "_player_buttons_frame"):
            return
        self._player_buttons.clear()
        self._layout_player_buttons()

    def _update_player_button_states(self) -> None:
        """Refresh player button enabled/disabled based on URL and playlist."""
        state = "normal" if (self._url_var.get() or self._playlist_urls) else "disabled"
        for name, btn in self._player_buttons.items():
            try:
                btn.configure(state=state)
            except tk.TclError:
                pass

    def _on_players_changed(self) -> None:
        self._rebuild_player_buttons()
        names = list(self._config.get("players", {}).keys())
        self._auto_play_combo.configure(values=names)
        if self._auto_play_player_var.get() not in names:
            self._auto_play_player_var.set("")
        self._reset_mpv_state()

    def _open_player_dialog(self) -> None:
        """Open the player management dialog."""
        players = self._config.get("players", {})
        PlayerDialog(self, players, self._on_players_changed)

    def _on_record_click(self) -> None:
        url = self._url_var.get()
        if not url:
            return
        save_dir = self._save_dir_var.get().strip()
        dialog = RecordDialog(self, url, save_dir, stream_duration=self._stream_duration)
        result = dialog.show()
        if result:
            rec_url, output, duration = result
            self._start_recording(rec_url, output, duration)

    def _capture_thread_fn(
        self, name: str, ports: dict[str, int], protocols: list[str],
        bind_ip: str = "",
        audio_output: str | None = None, audio_duration: float | None = None,
        continuous: bool = False,
    ) -> None:
        try:
            url = capture(
                name=name, port=ports.get("dlna", 9090),
                airplay_port=ports.get("airplay", 9091), cast_port=ports.get("cast", 8009),
                protocols=protocols, stop_event=self._stop_event,
                on_url=lambda u: self._call_ui(self._on_url, u),
                on_play=lambda: self._call_ui(self._on_auto_play),
                audio_output=audio_output, audio_duration=audio_duration,
                bind_ip=bind_ip or None,
                continuous=continuous,
            )
            if url is None:
                pass
        except Exception as exc:
            log.error("捕获失败: %s", exc)
            self._call_ui(self._show_status, f"错误: {exc}", "red")
        finally:
            self._call_ui(self._set_running, False)

    def _on_url(self, url: str) -> None:
        self._load_url_to_ui(url)
        if self._continuous_var.get():
            self._capture_count += 1
            self._show_status(f"● 持续捕获中 ({self._capture_count})", "green")
        elif self._auto_play_var.get():
            player_name = self._auto_play_player_var.get()
            if player_name and url:
                self._schedule_auto_play(player_name, url)

    def _schedule_auto_play(self, player_name: str, url: str) -> None:
        def do_launch():
            try:
                now = time.time()
                if url == self._auto_play_last_url and now - self._auto_play_last_ts < 3.0:
                    return
                self._auto_play_last_url = url
                self._auto_play_last_ts = now
                self._launch_auto_player(player_name, url)
            except Exception as exc:
                log.error("自动播放失败: %s", exc)
        self.after(100, do_launch)

    def _on_auto_play(self) -> None:
        if not self._auto_play_var.get():
            return
        if not self._continuous_var.get():
            return
        player_name = self._auto_play_player_var.get()
        url = self._url_var.get()
        if not player_name or not url:
            return
        self._launch_auto_player(player_name, url)

    def _load_url_to_ui(self, url: str, record_history: bool = True, remark: str = "", from_history: bool = False) -> None:
        """Load a URL into the capture card UI."""
        self._url_var.set(url)
        self._update_player_button_states()
        self._info_var.set("获取流信息中...")
        self._remark_var.set(remark)
        self._update_remark_mode()
        if from_history:
            self._url_prefix_var.set("历史URL:")
            self._url_prefix_label.configure(foreground="green")
        else:
            self._url_prefix_var.set("捕获URL:")
            self._url_prefix_label.configure(foreground="blue")
            self._show_status("● 已捕获", "blue")
        self._rec_status_var.set("")
        self._rec_btn.configure(state="normal")
        for btn in self._player_buttons.values():
            btn.configure(state="normal")
        # Snapshot the remark on the Tk thread: _fetch_stream_info runs on a
        # worker thread and must not touch tkinter variables.
        threading.Thread(
            target=self._fetch_stream_info,
            args=(url, record_history, self._remark_var.get()),
            daemon=True,
        ).start()

    def _fetch_stream_info(self, url: str, record_history: bool = True, remark: str = "") -> None:
        """Probe *url* for stream metadata (worker thread — no Tk access)."""
        data = _get_stream_info(url)
        if data.get("error"):
            log.warning("流信息解析不可用: %s", data["error"])
            self._stream_duration = None
            self._call_ui(self._info_var.set,
                          "流信息: 解析失败（未找到 ffprobe，可在 config.json 配置 ffmpeg_dir，或安装 ffmpeg 到 PATH）")
            if record_history:
                _add_history_record(url, {}, remark=remark)
            return
        info = _parse_stream_info(data)
        duration_str = info.get("duration", "未知")
        if duration_str not in ("未知", "直播中"):
            try:
                parts = duration_str.split(":")
                if len(parts) == 3:
                    # HH:MM:SS 格式
                    self._stream_duration = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
                elif len(parts) == 2:
                    # MM:SS 格式
                    self._stream_duration = float(parts[0]) * 60 + float(parts[1])
                else:
                    self._stream_duration = None
            except (ValueError, IndexError):
                self._stream_duration = None
        else:
            self._stream_duration = None
        line1, line2 = _format_stream_info(info)
        combined = f"{line1}  {line2}" if line2 else line1
        self._call_ui(self._info_var.set, combined)
        # Cache the result so playlist entries and later probes reuse it.
        self._call_ui(self._apply_probe_result, url, info)

        # Save to history
        if record_history:
            _add_history_record(url, info, remark=remark)

    def _update_remark_mode(self) -> None:
        for child in self._remark_frame.winfo_children():
            child.destroy()
        lbl = ttk.Label(self._remark_frame, textvariable=self._remark_var, foreground="blue")
        lbl.pack(fill="x")
        remark = self._remark_var.get()
        self._remark_btn.configure(text="编辑备注" if remark else "添加备注")

    def _edit_remark_dialog(self) -> None:
        url = self._url_var.get()
        if not url:
            _show_toast(self, "请先捕获 URL")
            return
        dialog = tk.Toplevel(self)
        dialog.title("编辑备注")
        dialog.resizable(False, False)
        frm = ttk.Frame(dialog, padding=16)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="备注:").pack(anchor="w")
        var = tk.StringVar(value=self._remark_var.get())
        entry = ttk.Entry(frm, textvariable=var, width=40)
        entry.pack(fill="x", pady=(4, 12))
        entry.focus_set()
        entry.selection_range(0, "end")
        btn_frm = ttk.Frame(frm)
        btn_frm.pack(fill="x")

        def on_save() -> None:
            remark = var.get().strip()
            self._remark_var.set(remark)
            if url:
                records = _load_history()
                for record in reversed(records):
                    if record["url"] == url:
                        record["remark"] = remark
                        break
                _save_history(records)
            self._update_remark_mode()
            dialog.destroy()

        def on_cancel() -> None:
            dialog.destroy()

        ttk.Button(btn_frm, text="取消", command=on_cancel, width=8).pack(side="right", padx=(4, 0))
        ttk.Button(btn_frm, text="保存", command=on_save, width=8).pack(side="right")
        entry.bind("<Return>", lambda e: on_save())

        dialog.update_idletasks()
        dw, dh = dialog.winfo_width(), dialog.winfo_height()
        x = self.winfo_rootx() + (self.winfo_width() - dw) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dh) // 2
        dialog.geometry(f"+{x}+{y}")
        dialog.grab_set()
        dialog.wait_window()
        self.focus_set()

    def _start_recording(self, url: str, output: str, duration: float | None = None) -> None:
        ffmpeg = _find_tool("ffmpeg")
        if not ffmpeg:
            _show_error(self, "错误", "未找到 ffmpeg/ffprobe，请在 config.json 配置 ffmpeg_dir，或安装 ffmpeg 到 PATH")
            return
        cmd = _build_record_cmd(ffmpeg, url, output, duration)
        log.info("开始录制 → %s", output)
        self._recording = True
        self._stop_rec_btn.configure(state="normal")
        self._rec_btn.configure(state="disabled")
        self._rec_status_var.set(f"● 录制中  {Path(output).name}")
        self._progress_var.set("进度: 准备中...")
        self._progress_pct_var.set("0%")
        self._progress_bar["value"] = 0

        def _run_record():
            try:
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE, universal_newlines=True, bufsize=1,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
                self._record_proc = proc
                stderr_lines = []
                for line in proc.stderr:
                    stderr_lines.append(line.rstrip())
                    info = _parse_ffmpeg_progress(line)
                    if info:
                        self._call_ui(self._update_progress, info, duration)
                proc.wait()
                if proc.returncode == 0 or proc.returncode == 255:
                    self._call_ui(log.info, "录制完成 → %s", output)
                    self._call_ui(self._rec_status_var.set, f"● 录制完成  {Path(output).name}")
                    self._call_ui(lambda: self._progress_var.set(re.sub(
                        r' \| 速度: [\d.]+x', ' | 速度: 0x', self._progress_var.get()
                    )))
                    self._call_ui(lambda u=url, o=output: _mark_history_record(u, o))
                else:
                    for err_line in stderr_lines:
                        if not err_line.startswith(("frame=", "size=", "speed=", "Output", "Stream mapping", "Press", "  ")):
                            self._call_ui(log.warning, "  ffmpeg: %s", err_line)

                    self._call_ui(log.warning, "录制进程退出，返回码 %d (0x%08X)", proc.returncode, proc.returncode)
                    self._call_ui(lambda: self._rec_status_var.set(f"● 录制失败 {Path(output).name} (返回码 {proc.returncode})"))
            except Exception as exc:
                self._call_ui(log.error, "录制失败: %s", exc)
                self._call_ui(self._rec_status_var.set, "● 录制失败")
            finally:
                self._record_proc = None
                self._recording = False
                self._call_ui(lambda: self._stop_rec_btn.configure(state="disabled"))
                self._call_ui(lambda: self._rec_btn.configure(state="normal") if self._url_var.get() else None)

        threading.Thread(target=_run_record, daemon=True).start()

    def _update_progress(self, info: dict, total_duration: float | None) -> None:
        effective_total = total_duration or self._stream_duration
        if self._stream_duration and total_duration and total_duration > self._stream_duration:
            effective_total = self._stream_duration
        parts = []
        if "time" in info:
            parts.append(f"时间: {info['time']}")
            if effective_total:
                parts.append(f" / 总时长: {_format_duration(effective_total)}")
        if "size" in info:
            parts.append(f" | 大小: {_format_size(info['size'])}")
        if "speed" in info:
            parts.append(f" | 速度: {info['speed']}")
        self._progress_var.set("".join(parts))
        if effective_total and "time" in info:
            pct = min((_parse_time_to_seconds(info["time"]) / effective_total) * 100, 100)
            self._progress_bar["value"] = pct
            self._progress_pct_var.set(f"{pct:.0f}%")
        else:
            self._progress_pct_var.set("")

    def _stop_recording(self) -> None:
        proc = self._record_proc
        if proc and proc.poll() is None:
            try:
                proc.stdin.write("q\n")
                proc.stdin.close()
            except Exception:
                pass
            log.info("正在停止录制...")

    def _set_running(self, running: bool) -> None:
        self._running = running
        if running:
            self._set_run_state("running")
            self._show_status("● 运行中...", "green")
            # Disable device settings
            self._name_entry.configure(state="disabled")
            self._net_combo.configure(state="disabled")
            for proto in PROTOCOLS:
                self._proto_checkbuttons[proto].configure(state="disabled")
                self._port_entries[proto].configure(state="disabled")
            self._restore_btn.configure(state="disabled")
            self._continuous_cb.configure(state="disabled")
        else:
            self._set_run_state("idle")
            # Enable device settings
            self._name_entry.configure(state="normal")
            self._net_combo.configure(state="readonly")
            for proto in PROTOCOLS:
                self._proto_checkbuttons[proto].configure(state="normal")
                self._port_entries[proto].configure(state="normal")
            self._restore_btn.configure(state="normal")
            self._continuous_cb.configure(state="normal")
            cnt = self._capture_count
            if cnt and self._continuous_var.get():
                self._show_status(f"● 已停止 (共 {cnt} 个)", "red")
            else:
                self._show_status("● 已停止", "red")

    def _call_ui(self, fn: Callable, *args) -> None:
        self._ui_queue.put((fn, args))

    def _drain_pending_calls(self) -> None:
        if self._closing:
            return
        while True:
            try:
                fn, args = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except Exception:
                pass
        self._drain_job = self.after(50, self._drain_pending_calls)

    def _show_status(self, text: str, color: str) -> None:
        self._status_var.set(text)
        self._status_label.configure(foreground=color)

    def _copy_url(self) -> None:
        url = self._url_var.get()
        if url:
            self.clipboard_clear()
            self.clipboard_append(url)

    @staticmethod
    def _add_log_copy_menu(text_widget: tk.Text) -> None:
        """Add right-click context menu with Copy and Select All to a log text widget."""
        menu = tk.Menu(text_widget, tearoff=0)
        menu.add_command(label="复制", command=lambda: text_widget.event_generate("<<Copy>>"))
        menu.add_separator()
        menu.add_command(label="全选", command=lambda: text_widget.tag_add("sel", "1.0", "end"))
        def _show_menu(event):
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()
        text_widget.bind("<Button-3>", _show_menu)

    @staticmethod
    def _block_log_edit_keys(event: tk.Event) -> str | None:
        """Block editing keys but allow copy, select-all, navigation."""
        if event.state & 0x4 and event.keysym.lower() == "c":
            return None
        if event.state & 0x4 and event.keysym.lower() == "a":
            event.widget.tag_add("sel", "1.0", "end")
            return "break"
        if event.keysym in ("Up", "Down", "Left", "Right", "Home", "End",
                           "Prior", "Next", "Tab", "Escape", "F5"):
            return None
        return "break"

    def _switch_log_tab(self) -> None:
        tab = self._log_tab_var.get()
        self._log_tab = tab
        if tab == "current":
            self._history_log_frame.pack_forget()
            self._current_log_frame.pack(fill="both", expand=True)
            self._clear_btn.configure(text="清除当前日志")
        else:
            self._current_log_frame.pack_forget()
            self._history_log_frame.pack(fill="both", expand=True)
            self._clear_btn.configure(text="清除历史日志")
            if not self._history_loaded:
                self._load_history_logs()
                self._history_loaded = True

    def _clear_log(self) -> None:
        """Clear the currently visible log view and log files."""
        if self._log_tab == "current":
            log_text = self._log_text
            msg = "确定要清除当前日志显示和本次运行的日志文件吗？"
            mode = "current"
        else:
            log_text = self._history_log_text
            msg = "确定要清除历史日志显示和历史日志文件吗？"
            mode = "history"

        # Custom confirmation dialog centered on main window
        dialog = tk.Toplevel(self)
        dialog.title("确认")
        dialog.resizable(False, False)
        dialog.grab_set()

        msg_frame = ttk.Frame(dialog, padding=20)
        msg_frame.pack(fill="both", expand=True)
        ttk.Label(msg_frame, text=msg, font=("Microsoft YaHei", 10)).pack()

        btn_frame = ttk.Frame(dialog, padding=(0, 10, 0, 15))
        btn_frame.pack(fill="x")

        result = [False]

        def on_yes():
            result[0] = True
            dialog.destroy()

        def on_no():
            dialog.destroy()

        ttk.Button(btn_frame, text="是", command=on_yes, width=8).pack(side="right", padx=(0, 20))
        ttk.Button(btn_frame, text="否", command=on_no, width=8).pack(side="right", padx=(0, 10))

        dialog.update_idletasks()
        dialog_w = dialog.winfo_width()
        dialog_h = dialog.winfo_height()
        x = self.winfo_x() + (self.winfo_width() - dialog_w) // 2
        y = self.winfo_y() + (self.winfo_height() - dialog_h) // 2
        dialog.geometry(f"+{x}+{y}")

        dialog.wait_window()
        self.focus_set()

        if result[0]:
            log_text.delete("1.0", "end")
            if mode == "current":
                self._text_handler.reset_counter()
            _clear_log_files(mode=mode, current_file=self._current_log_file)
            if mode == "current":
                # Continue logging into a brand new file, otherwise the next
                # record re-creates the file we just removed.
                self._current_log_file = self._text_handler.rotate()

    def _load_history_logs(self) -> None:
        """Load historical logs into history log text widget."""
        recent = _load_recent_logs(500)
        if recent:
            for idx, line in enumerate(recent):
                tag = "log_even" if idx % 2 == 0 else "log_odd"
                self._history_log_text.insert("end", line + "\n\n", tag)
            self._history_log_text.see("end")
        else:
            _show_toast(self, "没有找到历史日志")


_FT_FILE = None


def _enable_crash_dumps():
    """Enable hard-crash stack dumps; returns the dump file or None."""
    global _FT_FILE
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        _cleanup_stale_files()
        path = LOG_DIR / f"faulthandler-{time.strftime('%Y%m%d-%H%M%S')}.txt"
        _FT_FILE = path.open("a")
        # Dump only when the process actually crashes. The previous call was
        # dump_traceback_later(45), which wrote a "Timeout (0:00:45)!" dump on
        # every healthy run and hid real signals in noise.
        faulthandler.enable(file=_FT_FILE, all_threads=True)
        return _FT_FILE
    except Exception:
        return None


def _start_hang_watchdog(app: App, ft_file, threshold: float = 90.0, interval: float = 5.0) -> None:
    """Dump all thread stacks when the Tk main loop stops responding.

    A heartbeat driven by ``after`` runs on the UI thread, so a growing gap
    means that thread is blocked. Only one dump is written per stall, and no
    Tk call is ever made from the watchdog thread (that would itself block).
    """
    state = {"beat": time.monotonic()}

    def _beat() -> None:
        state["beat"] = time.monotonic()
        try:
            app.after(int(interval * 1000), _beat)
        except (tk.TclError, RuntimeError):
            pass

    def _watch() -> None:
        while True:
            time.sleep(interval)
            if getattr(app, "_closing", False):
                return
            lag = time.monotonic() - state["beat"]
            if lag < threshold:
                continue
            try:
                log.warning("界面已无响应 %.0f 秒，转储全部线程栈到 %s", lag, ft_file.name)
                faulthandler.dump_traceback(file=ft_file, all_threads=True)
            except Exception:
                pass
            time.sleep(threshold)

    try:
        app.after(0, _beat)
    except (tk.TclError, RuntimeError):
        return
    threading.Thread(target=_watch, daemon=True).start()


def run() -> None:
    """Entry point for the GUI."""
    ft_file = _enable_crash_dumps()
    app = App()
    if ft_file is not None:
        _start_hang_watchdog(app, ft_file)
    app.mainloop()


if __name__ == "__main__":
    run()
