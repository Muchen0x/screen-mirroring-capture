"""Single-instance guard for the GUI.

Two windows would fight over the same device name and the same ports, so a
second launch must not start a second app: it asks the running instance to come
to the front and exits with :data:`EXIT_ALREADY_RUNNING`.

Windows uses two kernel objects:

* a named mutex — whoever creates it first owns the instance.  The OS releases
  it when the process dies, so a crashed instance never blocks the next start.
* a named auto-reset event — carries the "come forward" request.  It stays
  signaled until the owner consumes it, so launching during a slow startup
  still raises the window.

Other platforms fall back to an advisory ``flock`` on a file in the temp dir:
a second window is prevented, but cross-process activation is Windows-only.

Only the GUI is guarded — the CLI capture can legitimately run several times.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
import time

log = logging.getLogger(__name__)

#: Exit status of a launch that found another instance and activated it.
EXIT_ALREADY_RUNNING = 3

_APP_KEY = "screen_mirroring_capture_gui"
_MUTEX_NAME = "Local\\" + _APP_KEY
_EVENT_NAME = "Local\\" + _APP_KEY + "_activate"

_IS_WINDOWS = sys.platform == "win32"

# Win32 constants (kept local so the module imports cleanly off-Windows)
_ERROR_ALREADY_EXISTS = 183
_EVENT_MODIFY_STATE = 0x0002
_WAIT_OBJECT_0 = 0x0000
_WAIT_TIMEOUT = 0x0102
_GA_ROOT = 2
_SW_RESTORE = 9
_VK_MENU = 0x12
_KEYEVENTF_KEYUP = 0x0002

_k32 = None
_u32 = None


def _apis():
    """Load and cache kernel32/user32 with correct 64-bit signatures."""
    global _k32, _u32
    if _k32 is not None:
        return _k32, _u32

    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    u32 = ctypes.WinDLL("user32", use_last_error=True)

    # Handles are 64-bit: without explicit restypes ctypes truncates them to int.
    k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL,
                                 wintypes.LPCWSTR]
    k32.CreateEventW.restype = wintypes.HANDLE
    k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k32.OpenEventW.restype = wintypes.HANDLE
    k32.SetEvent.argtypes = [wintypes.HANDLE]
    k32.SetEvent.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD

    u32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    u32.GetAncestor.restype = wintypes.HWND
    u32.IsIconic.argtypes = [wintypes.HWND]
    u32.IsIconic.restype = wintypes.BOOL
    u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u32.ShowWindow.restype = wintypes.BOOL
    u32.SetForegroundWindow.argtypes = [wintypes.HWND]
    u32.SetForegroundWindow.restype = wintypes.BOOL
    u32.BringWindowToTop.argtypes = [wintypes.HWND]
    u32.BringWindowToTop.restype = wintypes.BOOL
    u32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD,
                                ctypes.POINTER(wintypes.ULONG)]
    u32.GetForegroundWindow.restype = wintypes.HWND
    u32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    u32.FindWindowW.restype = wintypes.HWND

    _k32, _u32 = k32, u32
    return k32, u32


def window_handle(widget) -> int:
    """Return the top-level HWND behind a Tk widget (0 when unavailable)."""
    if not _IS_WINDOWS or widget is None:
        return 0
    try:
        _, u32 = _apis()
        hwnd = int(widget.winfo_id())
    except Exception:
        return 0
    root = int(u32.GetAncestor(hwnd, _GA_ROOT) or 0)
    return root or hwnd


def find_window(title: str) -> int:
    """Find a top-level window by exact title (0 when not found)."""
    if not _IS_WINDOWS or not title:
        return 0
    try:
        _, u32 = _apis()
        return int(u32.FindWindowW(None, title) or 0)
    except Exception:
        return 0


def force_foreground(hwnd: int) -> bool:
    """Restore *hwnd* if minimized and bring it to the foreground.

    ``SetForegroundWindow`` is refused when the caller does not own the current
    foreground, so a synthetic ALT press is used to satisfy that rule before
    retrying — the same trick every single-instance helper uses on Windows.
    """
    if not _IS_WINDOWS or not hwnd:
        return False
    try:
        _, u32 = _apis()
        if u32.IsIconic(hwnd):
            u32.ShowWindow(hwnd, _SW_RESTORE)
        if u32.SetForegroundWindow(hwnd):
            u32.BringWindowToTop(hwnd)
            return True
        u32.keybd_event(_VK_MENU, 0, 0, None)
        u32.keybd_event(_VK_MENU, 0, _KEYEVENTF_KEYUP, None)
        ok = bool(u32.SetForegroundWindow(hwnd))
        if ok:
            u32.BringWindowToTop(hwnd)
        return ok
    except Exception:
        log.debug("前置窗口失败", exc_info=True)
        return False


class SingleInstance:
    """Owns the "only one GUI" claim for the lifetime of the object."""

    def __init__(self, key: str = "") -> None:
        del key  # reserved: names are process-wide on purpose
        self._mutex = None
        self._event = None
        self._lock_file = None
        self._claimed = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── claim ────────────────────────────────────────────────────
    def acquire(self) -> bool:
        """Try to become the running instance.

        Returns ``True`` when this process owns the instance (or when the guard
        cannot work at all — a broken guard must never block startup).
        """
        if self._claimed:
            return True
        self._stop.clear()
        if _IS_WINDOWS:
            self._claimed = self._acquire_windows()
        else:
            self._claimed = self._acquire_posix()
        return self._claimed

    def _acquire_windows(self) -> bool:
        import ctypes

        k32, _ = _apis()
        self._mutex = k32.CreateMutexW(None, False, _MUTEX_NAME)
        if not self._mutex:
            log.warning("无法创建单例互斥体（err=%s），按多实例运行",
                        ctypes.get_last_error())
            return True
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            k32.CloseHandle(self._mutex)
            self._mutex = None
            return False
        # Auto-reset event: the owner consumes each request exactly once.
        self._event = k32.CreateEventW(None, False, False, _EVENT_NAME)
        if not self._event:
            log.warning("无法创建单例事件（err=%s），无法响应重复启动",
                        ctypes.get_last_error())
        return True

    def _acquire_posix(self) -> bool:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - non-POSIX
            return True
        path = os.path.join(tempfile.gettempdir(), _APP_KEY + ".lock")
        try:
            fh = open(path, "a+")
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                fh.close()
            except Exception:
                pass
            return False
        self._lock_file = fh
        return True

    # ── owner side ───────────────────────────────────────────────
    def listen(self, on_activate, poll: float = 0.2) -> threading.Thread | None:
        """Start a thread that calls *on_activate* when a launch asks for it.

        *on_activate* runs on that thread, so it must marshal to the UI thread
        itself (the app queues it via ``_call_ui``).
        """
        if not _IS_WINDOWS or not self._event:
            return None
        event = self._event

        def _loop() -> None:
            k32, _ = _apis()
            while not self._stop.is_set():
                rc = k32.WaitForSingleObject(event, int(poll * 1000))
                if rc == _WAIT_OBJECT_0:
                    if self._stop.is_set():
                        return  # woken only to unblock the wait on shutdown
                    log.info("收到重复启动请求，前置窗口")
                    try:
                        on_activate()
                    except Exception:
                        log.exception("前置窗口回调失败")
                elif rc != _WAIT_TIMEOUT:
                    return  # handle closed: app is shutting down

        thread = threading.Thread(target=_loop, name="single-instance", daemon=True)
        self._thread = thread
        thread.start()
        return thread

    # ── launcher side ────────────────────────────────────────────
    def request_activate(self, attempts: int = 20, delay: float = 0.15) -> bool:
        """Ask the running instance to come forward.  True when it was told."""
        if not _IS_WINDOWS:
            return False
        k32, _ = _apis()
        for index in range(attempts):
            handle = k32.OpenEventW(_EVENT_MODIFY_STATE, False, _EVENT_NAME)
            if handle:
                k32.SetEvent(handle)
                k32.CloseHandle(handle)
                return True
            # The owner may still be starting up (mutex exists, event not yet).
            if index < attempts - 1:
                time.sleep(delay)
        return False

    def release(self) -> None:
        if not self._claimed:
            return
        k32 = _k32
        # Wake the listener before closing its handle: closing a handle another
        # thread is waiting on is undefined, so signal + join first.
        self._stop.set()
        if self._event and k32 is not None:
            try:
                k32.SetEvent(self._event)
            except Exception:
                pass
        if self._thread is not None:
            try:
                self._thread.join(0.5)
            except RuntimeError:  # joining ourselves
                pass
            self._thread = None
        for handle in (self._event, self._mutex):
            if handle and k32 is not None:
                try:
                    k32.CloseHandle(handle)
                except Exception:
                    pass
        self._event = self._mutex = None
        if self._lock_file is not None:
            try:
                self._lock_file.close()
            except Exception:
                pass
            self._lock_file = None
        self._claimed = False

    @property
    def claimed(self) -> bool:
        return self._claimed
