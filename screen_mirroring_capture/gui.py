"""tkinter GUI for screen-mirroring-capture.

Launch with::

    python -m screen_mirroring_capture --gui
    screen-mirroring-capture-gui
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sys
import signal
import subprocess
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from . import PROTOCOLS, capture
from .net import list_adapters

log = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────

CONFIG_DIR = Path.home() / ".screen-mirroring-capture"
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_DIR = CONFIG_DIR / "logs"
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

def _find_player_executable(path_or_cmd: str) -> str | None:
    """Find a player executable by command name or full path.

    Args:
        path_or_cmd: A command name (e.g. "vlc") or full path.

    Returns:
        Full path to the executable, or None if not found.
    """
    # Check if it's a full path that exists
    p = Path(path_or_cmd)
    if p.is_absolute() and p.exists():
        return str(p)
    # Search PATH
    found = shutil.which(path_or_cmd)
    if found:
        return found
    # Common fallback paths
    if "vlc" in path_or_cmd.lower():
        common = [
            Path("C:/Program Files/VideoLAN/VLC/vlc.exe"),
            Path("C:/Program Files (x86)/VideoLAN/VLC/vlc.exe"),
            Path.home() / "AppData/Local/Programs/VLC/vlc.exe",
        ]
        for fp in common:
            if fp.exists():
                return str(fp)
    elif "mpv" in path_or_cmd.lower():
        common = [
            Path("C:/mpv/mpv.exe"),
            Path.home() / "scoop/apps/mpv/current/mpv.exe",
        ]
        for fp in common:
            if fp.exists():
                return str(fp)
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
    if changed:
        _save_config(config)


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
    records = _load_history()
    record = {
        "id": len(records) + 1,
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
    _save_history(records)


def _mark_history_record(url: str, output_path: str) -> None:
    """Mark the most recent matching history record as recorded."""
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
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return None, "未知"
        cmd = [
            ffprobe, "-v", "quiet", "-print_format", "json",
            "-show_format", "-allowed_extensions", "ALL", "-extension_picky", "0", url,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
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
    """Get detailed stream information using ffprobe."""
    try:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return {}
        cmd = [
            ffprobe, "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", "-allowed_extensions", "ALL",
            "-extension_picky", "0", url,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
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


# ── Logging handler ──────────────────────────────────────────


class PersistentTextHandler(logging.Handler):
    """Logging handler that writes to both Text widget and log file."""

    def __init__(self, widget: tk.Text, root: tk.Tk):
        super().__init__()
        self._widget = widget
        self._root = root
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.log_file = LOG_DIR / f"session_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        if "Using proactor:" in msg:
            return
        self._root.after(0, self._append_ui, msg)
        self._append_file(msg)

    def _append_ui(self, msg: str) -> None:
        self._widget.configure(state="normal")
        self._widget.insert("end", msg + "\n")
        self._widget.see("end")
        self._widget.configure(state="disabled")

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
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfig(self._canvas_window, width=e.width))
        self._canvas.configure(yscrollcommand=self._scrollbar.set)
        self._scrollbar.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)
        self._inner.bind("<Enter>", lambda e: self._bind_mousewheel())
        self._inner.bind("<Leave>", lambda e: self._unbind_mousewheel())

    def _bind_mousewheel(self) -> None:
        self._canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        self._canvas.bind_all("<Button-4>", self._on_mousewheel)
        self._canvas.bind_all("<Button-5>", self._on_mousewheel)

    def _unbind_mousewheel(self) -> None:
        self._canvas.unbind_all("<MouseWheel>")
        self._canvas.unbind_all("<Button-4>")
        self._canvas.unbind_all("<Button-5>")

    def _on_mousewheel(self, event: tk.Event) -> None:
        if event.num == 4 or (hasattr(event, "delta") and event.delta > 0):
            self._canvas.yview_scroll(-1, "units")
        elif event.num == 5 or (hasattr(event, "delta") and event.delta < 0):
            self._canvas.yview_scroll(1, "units")

    @property
    def inner(self) -> ttk.Frame:
        return self._inner


# ── Record dialog ────────────────────────────────────────────


class RecordDialog(tk.Toplevel):
    """Dialog for configuring recording settings."""

    def __init__(self, parent: tk.Tk, url: str, save_dir: str,
                 stream_duration: float | None = None):
        super().__init__(parent)
        self.title("录制设置")
        self.resizable(True, True)
        self.minsize(400, 300)
        self.grab_set()

        self._url = url
        self._stream_duration = stream_duration
        self._result: tuple[str, float | None] | None = None

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

        # Save directory
        frm_dir = ttk.LabelFrame(content, text="存储目录", padding=8)
        frm_dir.pack(fill="x", **pad)
        row_dir = ttk.Frame(frm_dir)
        row_dir.pack(fill="x")
        self._dir_var = tk.StringVar(value=save_dir)
        ttk.Entry(row_dir, textvariable=self._dir_var, width=40).pack(side="left", fill="x", expand=True, padx=(0, 4))
        ttk.Button(row_dir, text="浏览...", command=self._browse_dir).pack(side="left")

        # Filename
        frm_name = ttk.LabelFrame(content, text="文件名", padding=8)
        frm_name.pack(fill="x", **pad)
        self._name_var = tk.StringVar(value=f"recording_{timestamp}")
        self._name_entry = ttk.Entry(frm_name, textvariable=self._name_var, width=40)
        self._name_entry.pack(fill="x")
        self._name_entry.bind("<KeyRelease>", lambda e: self._update_preview())

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
        self._hour_wheel = WheelSelector(frm_wheels, min_val=0, max_val=23)
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

        self.update_idletasks()
        dialog_w, dialog_h = 400, 600
        x = parent.winfo_x() + (parent.winfo_width() - dialog_w) // 2
        y = parent.winfo_y() + (parent.winfo_height() - dialog_h) // 2
        self.geometry(f"{dialog_w}x{dialog_h}+{x}+{y}")

    def _set_duration(self, h: int, m: int, s: int) -> None:
        self._hour_wheel.set(h)
        self._min_wheel.set(m)
        self._sec_wheel.set(s)
        self._on_wheel_change()

    def _on_wheel_change(self) -> None:
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

    def _on_format_change(self, event: tk.Event) -> None:
        idx = self._fmt_combo.current()
        if 0 <= idx < len(_FORMAT_OPTIONS):
            self._ext_var.set(_FORMAT_OPTIONS[idx][0])
            self._update_preview()

    def _update_preview(self) -> None:
        save_dir, filename, ext = self._dir_var.get().strip(), self._name_var.get().strip(), self._ext_var.get()
        self._preview_var.set(str(Path(save_dir) / f"{filename}{ext}") if save_dir and filename else "(请填写存储目录和文件名)")

    def _cancel(self) -> None:
        self._result = None
        self.destroy()

    def _confirm(self) -> None:
        save_dir, filename, ext = self._dir_var.get().strip(), self._name_var.get().strip(), self._ext_var.get()
        if not save_dir:
            _show_warning(self, "提示", "请选择存储目录")
            return
        if not filename:
            _show_warning(self, "提示", "请输入文件名")
            return
        h, m, s = self._hour_wheel.get(), self._min_wheel.get(), self._sec_wheel.get()
        duration = h * 3600 + m * 60 + s if (h > 0 or m > 0 or s > 0) else None
        Path(save_dir).mkdir(parents=True, exist_ok=True)
        self._result = (str(Path(save_dir) / f"{filename}{ext}"), duration)
        self.destroy()

    def show(self) -> tuple[str, float | None] | None:
        self.wait_window()
        return self._result


# ── History dialog ───────────────────────────────────────────


def _show_toast(parent: tk.Widget, message: str, duration: int = 1500) -> None:
    """Show auto-dismiss toast notification."""
    toast = tk.Toplevel(parent)
    toast.overrideredirect(True)
    toast.attributes("-topmost", True)

    toast_w, toast_h = 220, 50
    x = parent.winfo_rootx() + (parent.winfo_width() - toast_w) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - toast_h) // 2
    toast.geometry(f"{toast_w}x{toast_h}+{x}+{y}")

    tk.Label(toast, text=message, bg="#4a90d9", fg="white",
             font=("Microsoft YaHei", 10)).pack(fill="both", expand=True)
    toast.after(duration, toast.destroy)


def _show_warning(parent: tk.Widget, title: str, message: str) -> None:
    """Show warning dialog centered on parent."""
    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.resizable(False, False)
    dialog.grab_set()

    msg_frame = ttk.Frame(dialog, padding=20)
    msg_frame.pack(fill="both", expand=True)
    ttk.Label(msg_frame, text=message, font=("Microsoft YaHei", 10)).pack()

    btn_frame = ttk.Frame(dialog, padding=(0, 10, 0, 15))
    btn_frame.pack(fill="x")
    ttk.Button(btn_frame, text="确定", command=dialog.destroy, width=8).pack(side="right", padx=20)

    dialog.update_idletasks()
    dialog_w = dialog.winfo_width()
    dialog_h = dialog.winfo_height()
    x = parent.winfo_rootx() + (parent.winfo_width() - dialog_w) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - dialog_h) // 2
    dialog.geometry(f"+{x}+{y}")

    dialog.wait_window()


def _show_error(parent: tk.Widget, title: str, message: str) -> None:
    """Show error dialog centered on parent."""
    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.resizable(False, False)
    dialog.grab_set()

    msg_frame = ttk.Frame(dialog, padding=20)
    msg_frame.pack(fill="both", expand=True)
    ttk.Label(msg_frame, text=message, font=("Microsoft YaHei", 10)).pack()

    btn_frame = ttk.Frame(dialog, padding=(0, 10, 0, 15))
    btn_frame.pack(fill="x")
    ttk.Button(btn_frame, text="确定", command=dialog.destroy, width=8).pack(side="right", padx=20)

    dialog.update_idletasks()
    dialog_w = dialog.winfo_width()
    dialog_h = dialog.winfo_height()
    x = parent.winfo_rootx() + (parent.winfo_width() - dialog_w) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - dialog_h) // 2
    dialog.geometry(f"+{x}+{y}")

    dialog.wait_window()


def _ask_yes_no(parent: tk.Widget, title: str, message: str) -> bool:
    """Show yes/no confirmation dialog centered on parent. Returns True if Yes."""
    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.resizable(False, False)
    dialog.grab_set()

    msg_frame = ttk.Frame(dialog, padding=20)
    msg_frame.pack(fill="both", expand=True)
    ttk.Label(msg_frame, text=message, font=("Microsoft YaHei", 10)).pack()

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

        self._load_records()

        # Center dialog
        self.update_idletasks()
        dialog_w, dialog_h = 600, 450
        x = parent.winfo_x() + (parent.winfo_width() - dialog_w) // 2
        y = parent.winfo_y() + (parent.winfo_height() - dialog_h) // 2
        self.geometry(f"{dialog_w}x{dialog_h}+{x}+{y}")
        self.lift()
        self.focus()
        TreeviewToolTip(self._tree)

    def _load_records(self) -> None:
        for item in self._tree.get_children():
            self._tree.delete(item)
        for record in reversed(self._records):
            self._tree.insert("", "end", values=(
                record["time"],
                record["url"],
                record.get("duration", ""),
                record.get("resolution", ""),
                record.get("remark", ""),
            ), tags=(str(record["id"]),))

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
        if record:
            self._confirm_btn.configure(state="normal")
        else:
            self._confirm_btn.configure(state="disabled")

    def _on_double_click(self, event: tk.Event) -> None:
        record = self._get_selected_record()
        if record:
            self._parent._load_url_to_ui(record["url"], record_history=False, remark=record.get("remark", ""))
            self.destroy()

    def _confirm_and_close(self) -> None:
        record = self._get_selected_record()
        if record:
            self._parent._load_url_to_ui(record["url"], record_history=False, remark=record.get("remark", ""))
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

        players = self._parent._config.get("players", {})
        if players:
            play_menu = tk.Menu(menu, tearoff=0)
            for name in players:
                play_menu.add_command(
                    label=f"▶ {name}",
                    command=lambda n=name, u=record["url"]: self._parent._open_in_player(n, u)
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
        self._records = [r for r in self._records if r["id"] != record_id]
        _save_history(self._records)
        self._load_records()

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
            record["remark"] = var.get().strip()
            _save_history(self._records)
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

    def _record_selected_url(self) -> None:
        record = self._get_selected_record()
        if not record:
            return
        url = record["url"]
        self._parent._load_url_to_ui(url, record_history=False, remark=record.get("remark", ""))
        save_dir = self._parent._save_dir_var.get().strip()
        dialog = RecordDialog(self._parent, url, save_dir)
        result = dialog.show()
        if result:
            output, duration = result
            self.destroy()
            self._parent._start_recording(url, output, duration)

    def _clear_history(self) -> None:
        if _ask_yes_no(self, "确认", "确定要清空所有捕获历史记录吗？"):
            self._records = []
            _save_history([])
            self._load_records()


# ── Player dialog ──────────────────────────────────────────────


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

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", **pad)
        ttk.Button(btn_frame, text="添加", width=6, command=self._add).pack(side="left", padx=2)
        ttk.Button(btn_frame, text="编辑", width=6, command=self._edit).pack(side="left", padx=2)
        ttk.Button(btn_frame, text="删除", width=6, command=self._remove).pack(side="left", padx=2)

        self._listbox = tk.Listbox(self, height=6, font=("Microsoft YaHei", 9))
        self._listbox.pack(fill="both", expand=True, **pad)
        self._listbox.bind("<<ListboxSelect>>", self._on_select)
        ListboxToolTip(self._listbox)

        self._path_var = tk.StringVar()
        ttk.Label(self, textvariable=self._path_var, foreground="gray", wraplength=600).pack(fill="x", padx=8, pady=(0, 4))

        btn_close = ttk.Frame(self)
        btn_close.pack(fill="x", **pad)
        ttk.Button(btn_close, text="关闭", command=self.destroy).pack(side="right")

    def _refresh_list(self) -> None:
        self._listbox.delete(0, tk.END)
        for name, path in self._players.items():
            self._listbox.insert(tk.END, f"{name}  —  {path}")
        if self._listbox.size() > 0:
            self._listbox.selection_set(0)
            self._on_select()

    def _on_select(self, event=None) -> None:
        sel = self._listbox.curselection()
        if sel:
            text = self._listbox.get(sel[0])
            path = text.split("  —  ", 1)[1] if "  —  " in text else ""
            self._path_var.set(f"路径: {path}")
        else:
            self._path_var.set("")



    def _add(self) -> None:
        path = filedialog.askopenfilename(
            title="选择播放器可执行文件",
            filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")],
            parent=self,
        )
        if not path:
            return
        name = self._parent._ask_name_input("播放器名称", "请输入播放器显示名称：", Path(path).stem)
        if not name:
            return
        self._players[name] = path
        _save_config(self._parent._config)
        self._refresh_list()
        self._on_change()

    def _remove(self) -> None:
        sel = self._listbox.curselection()
        if not sel:
            _show_warning(self, "提示", "请先选择一个播放器")
            return
        text = self._listbox.get(sel[0])
        name = text.split("  —  ", 1)[0]
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
        sel = self._listbox.curselection()
        if not sel:
            _show_warning(self, "提示", "请先选择一个播放器")
            return
        text = self._listbox.get(sel[0])
        old_name = text.split("  —  ", 1)[0]
        old_path = self._players.get(old_name, "")

        dialog = tk.Toplevel(self)
        dialog.title("编辑播放器")
        dialog.minsize(420, 180)
        dialog.resizable(True, True)
        dialog.grab_set()
        dialog.update_idletasks()
        pw, ph = self.winfo_width(), self.winfo_height()
        px, py = self.winfo_x(), self.winfo_y()
        dw, dh = 460, 200
        dialog.geometry(f"{dw}x{dh}+{px + (pw - dw) // 2}+{py + (ph - dh) // 2}")

        ttk.Label(dialog, text="播放器名称:").pack(anchor="w", padx=10, pady=(10, 2))
        name_var = tk.StringVar(value=old_name)
        name_entry = ttk.Entry(dialog, textvariable=name_var)
        name_entry.pack(fill="x", padx=10, pady=(0, 5))
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
            dialog.destroy()

        def cancel():
            dialog.destroy()

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(pady=(10, 10))
        ttk.Button(btn_frame, text="确定", command=confirm).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="取消", command=cancel).pack(side="left", padx=4)
        name_entry.bind("<Return>", lambda e: confirm())

        self.wait_window(dialog)
        if not result.get("ok"):
            return

        new_name = result["name"]
        new_path = result["path"]
        del self._players[old_name]
        self._players[new_name] = new_path
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
        lbl = tk.Label(
            self._tw, text=text,             bg="#ffffff", fg="#333",
            font=("Microsoft YaHei", 9), padx=6, pady=2,
            relief="solid", borderwidth=1,
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
    """Hover tooltip showing the full text of the treeview cell under cursor."""

    def __init__(self, treeview: ttk.Treeview) -> None:
        self._tv = treeview
        self._tw: tk.Toplevel | None = None
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
        col_idx = int(col[1:]) - 1
        if 0 <= col_idx < len(values) and values[col_idx]:
            self._show(event, str(values[col_idx]))
        else:
            self.hide()

    def _on_leave(self, event: tk.Event) -> None:
        self.hide()

    def _show(self, event: tk.Event, text: str) -> None:
        self.hide()
        self._tw = tk.Toplevel(self._tv)
        self._tw.wm_overrideredirect(True)
        self._tw.wm_geometry(f"+{event.x_root + 12}+{event.y_root + 8}")
        lbl = tk.Label(
            self._tw, text=text, bg="#ffffff", fg="#333",
            font=("Microsoft YaHei", 9), padx=6, pady=2,
            relief="solid", borderwidth=1,
        )
        lbl.pack()

    def hide(self) -> None:
        if self._tw:
            self._tw.destroy()
            self._tw = None


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
        lbl = tk.Label(
            self._tw, text=text, bg="#ffffff", fg="#333",
            font=("Microsoft YaHei", 9), padx=6, pady=2,
            relief="solid", borderwidth=1,
        )
        lbl.pack()

    def hide(self) -> None:
        if self._tw:
            self._tw.destroy()
            self._tw = None


class App(tk.Tk):
    """Main application window."""

    MIN_WIDTH = 560
    MIN_HEIGHT = 480

    def __init__(self) -> None:
        super().__init__()
        self.title("screen-mirroring-capture v1.0.0")
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
        self.geometry("690x560")
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        style = ttk.Style()
        style.configure("TLabelframe.Label", font=("Microsoft YaHei", 11, "bold"))
        style.configure(".", focuscolor="")
        self.configure(bg="#e0e0e0")

        self._running = False
        self._recording = False
        self._stop_event = threading.Event()
        self._capture_thread: threading.Thread | None = None
        self._record_proc: subprocess.Popen | None = None
        self._stream_duration: float | None = None
        self._config = _load_config()
        self._history_loaded = False
        self._current_log_file: Path | None = None

        self._build_ui()
        self._remove_focus_ring()
        self._setup_logging()

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
        if self._current_log_file:
            _merge_session_to_history(self._current_log_file)
        self.destroy()

    def _build_ui(self) -> None:
        pad = dict(padx=8, pady=4)

        # ── 设备设置 ──
        dev_wrapper = tk.Frame(self, highlightbackground="#888", highlightthickness=1, bd=0, bg="#e0e0e0")
        dev_wrapper.pack(fill="x", **pad)
        frm_dev = ttk.LabelFrame(dev_wrapper, text="设备设置", padding=8)
        frm_dev.pack(fill="x", expand=True)

        row1 = ttk.Frame(frm_dev)
        row1.pack(fill="x")
        ttk.Label(row1, text="设备名称:").pack(side="left")
        self._name_var = tk.StringVar(value=self._config.get("device_name", "screen-mirroring-capture"))
        self._name_entry = ttk.Entry(row1, textvariable=self._name_var, width=30)
        self._name_entry.pack(side="left", padx=(4, 16))
        self._name_entry.bind("<FocusOut>", lambda e: self._save_device_settings())

        ttk.Label(row1, text="网络:").pack(side="left")
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
            width=36, state="readonly",
        )
        # Restore selection from config
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
        self._net_combo.pack(side="left", padx=(4, 0))
        self._net_combo.bind("<<ComboboxSelected>>", lambda e: (self._save_device_settings(), self._net_tt.hide(), self.focus()))
        self._net_tt = ToolTip(self._net_combo, lambda: self._bind_ip_var.get())
        self._net_combo.bind("<Button-1>", lambda e: self._net_tt.hide(), add="+")
        self._net_combo.bind("<FocusOut>", lambda e: self._net_tt.hide(), add="+")

        row_proto = ttk.Frame(frm_dev)
        row_proto.pack(fill="x", pady=(6, 0))
        self._proto_vars: dict[str, tk.BooleanVar] = {}
        self._port_vars: dict[str, tk.StringVar] = {}
        self._proto_checkbuttons: dict[str, ttk.Checkbutton] = {}
        self._port_entries: dict[str, ttk.Entry] = {}
        proto_defaults = {"dlna": 9090, "airplay": 9091, "cast": 8009}
        saved_protos = self._config.get("protocols", {})
        for proto in PROTOCOLS:
            proto_cfg = saved_protos.get(proto, {})
            enabled = proto_cfg.get("enabled", True)
            port = proto_cfg.get("port", proto_defaults[proto])
            var = tk.BooleanVar(value=enabled)
            self._proto_vars[proto] = var
            cb = ttk.Checkbutton(row_proto, text=proto.upper(), variable=var, command=self._save_device_settings)
            cb.pack(side="left", padx=(0, 2))
            self._proto_checkbuttons[proto] = cb
            ttk.Label(row_proto, text="端口:").pack(side="left")
            port_var = tk.StringVar(value=str(port))
            self._port_vars[proto] = port_var
            pe = ttk.Entry(row_proto, textvariable=port_var, width=6)
            pe.pack(side="left", padx=(2, 12))
            pe.bind("<FocusOut>", lambda e: self._save_device_settings())
            self._port_entries[proto] = pe
        self._restore_btn = ttk.Button(row_proto, text="恢复默认", command=self._restore_defaults)
        self._restore_btn.pack(side="left", padx=(8, 0))

        # Debug logging toggle (moved to log section)
        self._verbose_var = tk.BooleanVar(value=self._config.get("verbose", False))

        # ── 捕获 ──
        capture_wrapper = tk.Frame(self, highlightbackground="#888", highlightthickness=1, bd=0, bg="#e0e0e0")
        capture_wrapper.pack(fill="x", **pad)
        frm_capture = ttk.LabelFrame(capture_wrapper, text="捕获", padding=8)
        frm_capture.pack(fill="x", expand=True)

        row_ctrl = ttk.Frame(frm_capture)
        row_ctrl.pack(fill="x")
        self._start_btn = ttk.Button(row_ctrl, text="▶ 开始", command=self._start, width=10)
        self._start_btn.pack(side="left", padx=4)
        self._stop_btn = ttk.Button(row_ctrl, text="⏹ 停止", command=self._stop, width=10, state="disabled")
        self._stop_btn.pack(side="left", padx=4)
        ttk.Button(row_ctrl, text="📋 捕获历史", command=self._show_history, width=10).pack(side="left", padx=4)
        self._status_var = tk.StringVar(value="● 已停止")
        self._status_label = ttk.Label(row_ctrl, textvariable=self._status_var, foreground="red")
        self._status_label.pack(side="left", padx=16)

        row_url = ttk.Frame(frm_capture)
        row_url.pack(fill="x", pady=(8, 0))
        ttk.Label(row_url, text="URL:").pack(side="left")
        self._url_var = tk.StringVar()
        self._url_entry = ttk.Entry(row_url, textvariable=self._url_var, state="readonly")
        self._url_entry.pack(side="left", fill="x", expand=True, padx=4)
        ToolTip(self._url_entry, lambda: self._url_var.get())
        ttk.Button(row_url, text="📋 复制", command=self._copy_url).pack(side="left")

        frm_info = ttk.Frame(frm_capture)
        frm_info.pack(fill="x", pady=(4, 0))
        ttk.Label(frm_info, text="流信息:").pack(side="left")
        self._info_var = tk.StringVar(value="")
        ttk.Label(frm_info, textvariable=self._info_var, foreground="blue").pack(side="left", padx=(4, 0))

        # Remark display
        row_remark = ttk.Frame(frm_capture)
        row_remark.pack(fill="x", pady=(4, 0))
        ttk.Label(row_remark, text="备注:").pack(side="left")
        self._remark_var = tk.StringVar(value="")
        self._remark_frame = ttk.Frame(row_remark)
        self._remark_frame.pack(side="left", padx=(4, 0))
        self._remark_btn = ttk.Button(row_remark, text="", command=self._edit_remark_dialog, width=10)
        self._remark_btn.pack(side="left", padx=(4, 0))
        self._update_remark_mode()

        # Player buttons
        self._player_buttons_frame = ttk.Frame(frm_capture)
        self._player_buttons_frame.pack(fill="x", pady=(8, 0))
        self._player_buttons: dict[str, ttk.Button] = {}
        ttk.Label(self._player_buttons_frame, text="播放器:").pack(side="left")
        players = self._config.get("players", {})
        for name in players:
            btn = ttk.Button(self._player_buttons_frame, text=f"▶ {name}", command=lambda n=name: self._open_in_player(n), state="disabled")
            btn.pack(side="left", padx=2)
            self._player_buttons[name] = btn
        self._edit_btn = ttk.Button(self._player_buttons_frame, text="编辑", command=self._open_player_dialog)
        self._edit_btn.pack(side="left", padx=(4, 0))

        # ── 录制 ──
        rec_wrapper = tk.Frame(self, highlightbackground="#888", highlightthickness=1, bd=0, bg="#e0e0e0")
        rec_wrapper.pack(fill="x", **pad)
        frm_rec = ttk.LabelFrame(rec_wrapper, text="录制", padding=8)
        frm_rec.pack(fill="x", expand=True)

        row_rec = ttk.Frame(frm_rec)
        row_rec.pack(fill="x")
        ttk.Label(row_rec, text="默认保存目录:").pack(side="left")
        self._save_dir_var = tk.StringVar(value=self._config.get("save_dir", ""))
        self._save_dir_entry = ttk.Entry(row_rec, textvariable=self._save_dir_var, width=40)
        self._save_dir_entry.pack(side="left", padx=(4, 4), fill="x", expand=True)
        ttk.Button(row_rec, text="浏览", command=self._browse_dir).pack(side="left")

        row_rec_ctrl = ttk.Frame(frm_rec)
        row_rec_ctrl.pack(fill="x", pady=(6, 0))
        self._rec_btn = ttk.Button(row_rec_ctrl, text="录制", command=self._on_record_click, width=8, state="disabled")
        self._rec_btn.pack(side="left", padx=4)
        self._stop_rec_btn = ttk.Button(row_rec_ctrl, text="停止录制", command=self._stop_recording, width=10, state="disabled")
        self._stop_rec_btn.pack(side="left", padx=4)
        self._rec_status_var = tk.StringVar(value="")
        ttk.Label(row_rec_ctrl, textvariable=self._rec_status_var, foreground="purple").pack(side="left", padx=16)

        # AirPlay audio recording
        row_audio = ttk.Frame(frm_rec)
        row_audio.pack(fill="x", pady=(6, 0))
        self._audio_rec_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row_audio, text="录制 AirPlay 音频", variable=self._audio_rec_var).pack(side="left")
        self._audio_output_var = tk.StringVar(value=self._config.get("audio_output", ""))
        self._audio_output_entry = ttk.Entry(row_audio, textvariable=self._audio_output_var, width=30)
        self._audio_output_entry.pack(side="left", padx=(8, 0), fill="x", expand=True)
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

        # ── 日志 ──
        log_wrapper = tk.Frame(self, highlightbackground="#888", highlightthickness=1, bd=0, bg="#e0e0e0")
        log_wrapper.pack(fill="both", expand=True, **pad)
        frm_log = ttk.LabelFrame(log_wrapper, text="日志", padding=8)
        frm_log.pack(fill="both", expand=True)

        row_log = ttk.Frame(frm_log)
        row_log.pack(fill="x")
        self._verbose_cb = ttk.Checkbutton(row_log, text="启用调试日志", variable=self._verbose_var, command=self._on_verbose_change)
        self._verbose_cb.pack(side="left", padx=2)
        self._clear_btn = ttk.Button(row_log, text="清除当前日志", command=self._clear_log)
        self._clear_btn.pack(side="right", padx=2)

        # Notebook for current / history logs
        self._notebook = ttk.Notebook(frm_log)
        self._notebook.pack(fill="both", expand=True)
        self._notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self._current_log_frame = ttk.Frame(self._notebook)
        self._notebook.add(self._current_log_frame, text="当前日志")

        self._log_text = tk.Text(self._current_log_frame, height=8, wrap="word",
                                 state="disabled", relief="sunken", bd=1)
        self._log_text.pack(fill="both", expand=True)

        self._history_log_frame = ttk.Frame(self._notebook)
        self._notebook.add(self._history_log_frame, text="历史日志")

        self._history_log_text = tk.Text(self._history_log_frame, height=8, wrap="word",
                                         state="disabled", relief="sunken", bd=1)
        self._history_log_text.pack(fill="both", expand=True)

    def _browse_dir(self) -> None:
        path = filedialog.askdirectory(title="选择保存目录", parent=self)
        if path:
            self._save_dir_var.set(path)
            self._save_dir_changed()

    def _save_dir_changed(self) -> None:
        self._config["save_dir"] = self._save_dir_var.get().strip()
        _save_config(self._config)

    def _browse_audio_dir(self) -> None:
        path = filedialog.askdirectory(title="选择音频保存目录", parent=self)
        if path:
            self._audio_output_var.set(path)
            self._save_audio_output()

    def _save_audio_output(self) -> None:
        self._config["audio_output"] = self._audio_output_var.get().strip()
        _save_config(self._config)

    def _restore_defaults(self) -> None:
        self._name_var.set("screen-mirroring-capture")
        self._bind_ip_var.set("自动检测")
        for proto in PROTOCOLS:
            self._proto_vars[proto].set(True)
            defaults = {"dlna": 9090, "airplay": 9091, "cast": 8009}
            self._port_vars[proto].set(str(defaults[proto]))
        self._save_device_settings()

    def _save_device_settings(self) -> None:
        self._config["device_name"] = self._name_var.get().strip() or "screen-mirroring-capture"
        selected_label = self._bind_ip_var.get()
        self._config["bind_ip"] = self._adapter_map.get(selected_label, "")
        saved_protos = {}
        for proto in PROTOCOLS:
            saved_protos[proto] = {
                "enabled": self._proto_vars[proto].get(),
                "port": int(self._port_vars[proto].get().strip() or "0"),
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
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
        self._text_handler.setFormatter(formatter)
        logging.getLogger().addHandler(self._text_handler)
        self._current_log_file = self._text_handler.log_file

    def _show_history(self) -> None:
        HistoryDialog(self)

    def _start(self) -> None:
        self._save_device_settings()
        self._stop_event.clear()
        self._set_running(True)
        protos = [p for p in PROTOCOLS if self._proto_vars[p].get()]
        ports = {p: int(self._port_vars[p].get().strip()) for p in PROTOCOLS}
        bind_ip = self._adapter_map.get(self._bind_ip_var.get(), "")
        threading.Thread(
            target=self._capture_thread_fn,
            args=(self._config["device_name"], ports, protos, bind_ip),
            daemon=True,
        ).start()

    def _stop(self) -> None:
        self._stop_event.set()

    def _ask_name_input(self, title: str, prompt: str, default: str = "") -> str | None:
        """Simple dialog to ask for a text input."""
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.resizable(False, False)
        dialog.grab_set()
        # Center on parent
        dialog.update_idletasks()
        pw, ph = self.winfo_width(), self.winfo_height()
        px, py = self.winfo_x(), self.winfo_y()
        dw, dh = 300, 120
        dialog.geometry(f"{dw}x{dh}+{px + (pw - dw) // 2}+{py + (ph - dh) // 2}")
        result = {"value": None}

        ttk.Label(dialog, text=prompt, wraplength=280).pack(pady=(10, 5), padx=10)
        var = tk.StringVar(value=default)
        entry = ttk.Entry(dialog, textvariable=var, width=30)
        entry.pack(padx=10, pady=(0, 10))
        entry.select_range(0, tk.END)
        entry.focus_set()

        def confirm():
            result["value"] = var.get().strip()
            dialog.destroy()

        def cancel():
            dialog.destroy()

        entry.bind("<Return>", lambda e: confirm())
        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(pady=(0, 10))
        ttk.Button(btn_frame, text="确定", command=confirm).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="取消", command=cancel).pack(side="left", padx=4)

        self.wait_window(dialog)
        return result["value"] if result["value"] else None

    def _open_in_player(self, player_name: str, url: str | None = None) -> None:
        """Open captured URL in a specific player by name."""
        url = url or self._url_var.get()
        if not url:
            return
        players = self._config.get("players", {})
        path_or_cmd = players.get(player_name)
        if not path_or_cmd:
            _show_warning(self, "提示", f"播放器 {player_name} 未配置")
            return
        player_path = _find_player_executable(path_or_cmd)
        if not player_path:
            _show_warning(self, "提示", f"未找到 {player_name} ({path_or_cmd})，请检查路径或重新添加")
            return
        try:
            cmd = [player_path, url]
            if "ffplay" in Path(player_path).stem.lower() and url.lower().endswith((".m3u8", ".m3u")):
                cmd[1:1] = ["-allowed_extensions", "ALL", "-allowed_segment_extensions", "ALL", "-extension_picky", "0"]
            subprocess.Popen(cmd)
            log.info("用 %s 打开: %s", player_name, url)
        except Exception as exc:
            _show_error(self, "错误", f"启动播放器失败: {exc}")

    def _rebuild_player_buttons(self) -> None:
        """Rebuild the player buttons in the capture card."""
        if not hasattr(self, "_player_buttons_frame"):
            return
        for btn in self._player_buttons.values():
            btn.destroy()
        self._player_buttons.clear()
        self._edit_btn.pack_forget()
        state = "normal" if self._url_var.get() else "disabled"
        players = self._config.get("players", {})
        for name in players:
            btn = ttk.Button(self._player_buttons_frame, text=f"▶ {name}",
                             command=lambda n=name: self._open_in_player(n),
                             state=state)
            btn.pack(side="left", padx=2)
            self._player_buttons[name] = btn
        self._edit_btn.pack(side="left", padx=(4, 0))

    def _on_players_changed(self) -> None:
        self._rebuild_player_buttons()

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
            output, duration = result
            self._start_recording(url, output, duration)

    def _capture_thread_fn(
        self, name: str, ports: dict[str, int], protocols: list[str],
        bind_ip: str = "",
        audio_output: str | None = None, audio_duration: float | None = None,
    ) -> None:
        try:
            url = capture(
                name=name, port=ports.get("dlna", 9090),
                airplay_port=ports.get("airplay", 9091), cast_port=ports.get("cast", 8009),
                protocols=protocols, stop_event=self._stop_event,
                on_url=lambda u: self.after(0, self._on_url, u),
                audio_output=audio_output, audio_duration=audio_duration,
                bind_ip=bind_ip or None,
            )
            if url is None:
                self.after(0, self._show_status, "● 已停止", "red")
        except Exception as exc:
            log.error("捕获失败: %s", exc)
            self.after(0, self._show_status, f"错误: {exc}", "red")
        finally:
            self.after(0, self._set_running, False)

    def _on_url(self, url: str) -> None:
        self._load_url_to_ui(url)

    def _load_url_to_ui(self, url: str, record_history: bool = True, remark: str = "") -> None:
        """Load a URL into the capture card UI."""
        self._url_var.set(url)
        self._info_var.set("获取流信息中...")
        self._remark_var.set(remark)
        self._update_remark_mode()
        self._show_status("● 已捕获", "blue")
        self._rec_status_var.set("")
        self._rec_btn.configure(state="normal")
        for btn in self._player_buttons.values():
            btn.configure(state="normal")
        threading.Thread(target=self._fetch_stream_info, args=(url, record_history), daemon=True).start()

    def _fetch_stream_info(self, url: str, record_history: bool = True) -> None:
        data = _get_stream_info(url)
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
        self.after(0, self._info_var.set, combined)

        # Save to history
        if record_history:
            _add_history_record(url, info, remark=self._remark_var.get())

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

    def _start_recording(self, url: str, output: str, duration: float | None = None) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            _show_error(self, "错误", "未找到 ffmpeg，请先安装并添加到 PATH")
            return
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "info", "-stats",
               "-fflags", "nobuffer", "-thread_queue_size", "512",
               "-i", url, "-c", "copy"]
        if url.lower().endswith((".m3u8", ".m3u")):
            cmd[1:1] = ["-allowed_extensions", "ALL", "-allowed_segment_extensions", "ALL", "-extension_picky", "0"]
        if duration:
            cmd.extend(["-t", str(duration)])
        cmd.extend(["-y", output])
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
                                        stderr=subprocess.PIPE, universal_newlines=True, bufsize=1)
                self._record_proc = proc
                stderr_lines = []
                for line in proc.stderr:
                    stderr_lines.append(line.rstrip())
                    info = _parse_ffmpeg_progress(line)
                    if info:
                        self.after(0, self._update_progress, info, duration)
                proc.wait()
                if proc.returncode == 0 or proc.returncode == 255:
                    self.after(0, log.info, "录制完成 → %s", output)
                    self.after(0, self._rec_status_var.set, f"● 录制完成  {Path(output).name}")
                    self.after(0, lambda: self._progress_var.set(re.sub(
                        r' \| 速度: [\d.]+x', ' | 速度: 0x', self._progress_var.get()
                    )))
                    self.after(0, lambda u=url, o=output: _mark_history_record(u, o))
                else:
                    for err_line in stderr_lines:
                        if not err_line.startswith(("frame=", "size=", "speed=", "Output", "Stream mapping", "Press", "  ")):
                            self.after(0, log.warning, "  ffmpeg: %s", err_line)

                    self.after(0, log.warning, "录制进程退出，返回码 %d (0x%08X)", proc.returncode, proc.returncode)
                    self.after(0, lambda: self._rec_status_var.set(f"● 录制失败 {Path(output).name} (返回码 {proc.returncode})"))
            except Exception as exc:
                self.after(0, log.error, "录制失败: %s", exc)
                self.after(0, self._rec_status_var.set, "● 录制失败")
            finally:
                self._record_proc = None
                self._recording = False
                self.after(0, lambda: self._stop_rec_btn.configure(state="disabled"))
                self.after(0, lambda: self._rec_btn.configure(state="normal") if self._url_var.get() else None)

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
            self._start_btn.configure(state="disabled")
            self._stop_btn.configure(state="normal")
            self._show_status("● 运行中...", "green")
            # Disable device settings
            self._name_entry.configure(state="disabled")
            self._net_combo.configure(state="disabled")
            for proto in PROTOCOLS:
                self._proto_checkbuttons[proto].configure(state="disabled")
                self._port_entries[proto].configure(state="disabled")
            self._restore_btn.configure(state="disabled")
        else:
            self._start_btn.configure(state="normal")
            self._stop_btn.configure(state="disabled")
            # Enable device settings
            self._name_entry.configure(state="normal")
            self._net_combo.configure(state="readonly")
            for proto in PROTOCOLS:
                self._proto_checkbuttons[proto].configure(state="normal")
                self._port_entries[proto].configure(state="normal")
            self._restore_btn.configure(state="normal")

    def _show_status(self, text: str, color: str) -> None:
        self._status_var.set(text)
        self._status_label.configure(foreground=color)

    def _copy_url(self) -> None:
        url = self._url_var.get()
        if url:
            self.clipboard_clear()
            self.clipboard_append(url)

    def _on_tab_changed(self, event: tk.Event) -> None:
        """Update button text when tab changes."""
        selected_tab = self._notebook.select()
        current_tab = str(self._current_log_frame)
        if selected_tab == current_tab:
            self._clear_btn.configure(text="清除当前日志")
        else:
            self._clear_btn.configure(text="清除历史日志")
            # Load history logs lazily
            if not self._history_loaded:
                self._load_history_logs()
                self._history_loaded = True

    def _clear_log(self) -> None:
        """Clear the currently visible log view and log files."""
        selected_tab = self._notebook.select()
        current_tab = str(self._current_log_frame)
        if selected_tab == current_tab:
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

        if result[0]:
            # Clear display
            log_text.configure(state="normal")
            log_text.delete("1.0", "end")
            log_text.configure(state="disabled")
            # Clear log files
            _clear_log_files(mode=mode, current_file=self._current_log_file)

    def _load_history_logs(self) -> None:
        """Load historical logs into history log text widget."""
        recent = _load_recent_logs(500)
        if recent:
            self._history_log_text.configure(state="normal")
            for line in recent:
                self._history_log_text.insert("end", line + "\n")
            self._history_log_text.see("end")
            self._history_log_text.configure(state="disabled")
        else:
            _show_toast(self, "没有找到历史日志")


def run() -> None:
    """Entry point for the GUI."""
    app = App()
    app.mainloop()


if __name__ == "__main__":
    run()
