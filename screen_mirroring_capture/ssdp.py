"""SSDP (Simple Service Discovery Protocol) advertiser.

Handles multicast advertisement (NOTIFY) and M-SEARCH responses so
that DLNA controllers on the local network can discover our renderer.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from email.utils import formatdate

log = logging.getLogger(__name__)

MULTICAST_ADDR = "239.255.255.250"
MULTICAST_PORT = 1900

# Re-announce periodically so controllers do not forget us before the
# advertised max-age (1800s) expires. 300s is comfortably inside it and
# keeps multicast traffic low (the previous code re-announced on every
# 2s socket timeout).
NOTIFY_INTERVAL = 300.0

_SEARCH_MATCHERS = (
    "MediaRenderer",
    "ssdp:all",
    "rootdevice",
    "AVTransport",
    "RenderingControl",
    "ConnectionManager",
)

_NOTIFY_TYPES = [
    "upnp:rootdevice",
    "urn:schemas-upnp-org:device:MediaRenderer:1",
    "urn:schemas-upnp-org:service:AVTransport:1",
    "urn:schemas-upnp-org:service:RenderingControl:1",
    "urn:schemas-upnp-org:service:ConnectionManager:1",
]

_SEARCH_TARGETS = [
    "upnp:rootdevice",
    "urn:schemas-upnp-org:device:MediaRenderer:1",
    "urn:schemas-upnp-org:service:AVTransport:1",
]


class SSDPAdvertiser:
    """Broadcasts SSDP presence and answers discovery queries."""

    def __init__(self, device_uuid: str, location: str, local_ip: str):
        self._uuid = device_uuid
        self._location = location
        self._local_ip = local_ip
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._listening = False
        # Populated when 1900/UDP cannot be used; callers surface it to the user.
        self.error: str | None = None

    @property
    def listening(self) -> bool:
        """True when we can actually receive M-SEARCH queries."""
        return self._listening

    def start(self) -> None:
        """Open the socket synchronously, then start the announce loop.

        Opening the socket here (instead of inside the thread) means a bind
        failure is reported to the caller instead of dying silently in a
        background thread.
        """
        try:
            self._sock = self._open_listen_socket()
            self._listening = True
        except OSError as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            log.warning(
                "SSDP 无法监听 UDP %d（常见原因：Windows 的 SSDP 发现服务或其它 DLNA 软件已占用）: %s",
                MULTICAST_PORT,
                exc,
            )
            try:
                self._sock = self._open_send_socket()
            except OSError as exc2:
                self.error = f"{type(exc2).__name__}: {exc2}"
                log.error("SSDP 初始化失败，DLNA 设备广播不可用: %s", exc2)
                return
            log.warning(
                "SSDP 降级为仅广播模式：设备仍会定时通告，但可能无法响应控制器的搜索请求"
            )

        log.info(
            "SSDP 服务广播启动%s: uuid=%s location=%s ip=%s",
            "" if self._listening else "（仅广播）",
            self._uuid,
            self._location,
            self._local_ip,
        )
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        log.info("SSDP 服务广播停止")
        self._stop.set()

    def _open_listen_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.bind(("", MULTICAST_PORT))
            mreq = struct.pack(
                "4s4s",
                socket.inet_aton(MULTICAST_ADDR),
                socket.inet_aton(self._local_ip),
            )
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_MULTICAST_IF,
                socket.inet_aton(self._local_ip),
            )
            sock.settimeout(1.0)
            return sock
        except OSError:
            try:
                sock.close()
            except OSError:
                pass
            raise

    def _open_send_socket(self) -> socket.socket:
        """Socket that can only send announcements (no 1900 bind)."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            try:
                sock.setsockopt(
                    socket.IPPROTO_IP,
                    socket.IP_MULTICAST_IF,
                    socket.inet_aton(self._local_ip),
                )
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
            except OSError:
                pass
            sock.bind(("", 0))
            sock.settimeout(1.0)
            return sock
        except OSError:
            try:
                sock.close()
            except OSError:
                pass
            raise

    def _loop(self) -> None:
        sock = self._sock
        if sock is None:
            return
        try:
            self._notify(sock)
            last_notify = time.monotonic()
            while not self._stop.is_set():
                if self._listening:
                    try:
                        data, addr = sock.recvfrom(4096)
                    except socket.timeout:
                        pass
                    except OSError as exc:
                        if self._stop.is_set():
                            break
                        self._listening = False
                        self.error = f"recv: {exc}"
                        log.warning("SSDP 接收失败，停止监听（保留广播）: %s", exc)
                    else:
                        self._handle_datagram(sock, data, addr)
                else:
                    self._stop.wait(5.0)

                now = time.monotonic()
                if now - last_notify >= NOTIFY_INTERVAL:
                    self._notify(sock)
                    last_notify = now
        except Exception:
            log.warning("SSDP 广播线程异常退出", exc_info=True)
        finally:
            try:
                sock.close()
            except OSError:
                pass
            log.debug("SSDP 循环退出")

    def _handle_datagram(self, sock: socket.socket, data: bytes, addr: tuple) -> None:
        msg = data.decode("utf-8", errors="replace")
        if "M-SEARCH" not in msg:
            return
        if not any(t in msg for t in _SEARCH_MATCHERS):
            return
        log.debug("SSDP M-SEARCH 来自 %s", addr)
        self._respond(sock, addr)

    def _notify(self, sock: socket.socket) -> None:
        for nt in [self._uuid] + _NOTIFY_TYPES:
            # UPnP spec: when NT is the device UUID itself, USN = UUID only
            usn = self._uuid if nt == self._uuid else f"{self._uuid}::{nt}"
            msg = (
                f"NOTIFY * HTTP/1.1\r\n"
                f"HOST: {MULTICAST_ADDR}:{MULTICAST_PORT}\r\n"
                f"CACHE-CONTROL: max-age=1800\r\n"
                f"LOCATION: {self._location}\r\n"
                f"NT: {nt}\r\n"
                f"NTS: ssdp:alive\r\n"
                f"USN: {usn}\r\n"
                f"SERVER: Linux/4.9 UPnP/1.0 DLNADOC/1.50 Xiaomi-DLNA/1.0\r\n"
                f"\r\n"
            )
            try:
                sock.sendto(msg.encode(), (MULTICAST_ADDR, MULTICAST_PORT))
            except OSError as exc:
                log.debug("SSDP NOTIFY 发送失败: %s", exc)

    def _respond(self, sock: socket.socket, addr: tuple) -> None:
        date = formatdate(timeval=None, localtime=False, usegmt=True)
        for st in _SEARCH_TARGETS:
            msg = (
                f"HTTP/1.1 200 OK\r\n"
                f"CACHE-CONTROL: max-age=1800\r\n"
                f"DATE: {date}\r\n"
                f"LOCATION: {self._location}\r\n"
                f"ST: {st}\r\n"
                f"USN: {self._uuid}::{st}\r\n"
                f"SERVER: Linux/4.9 UPnP/1.0 DLNADOC/1.50 Xiaomi-DLNA/1.0\r\n"
                f"EXT:\r\n"
                f"\r\n"
            )
            try:
                sock.sendto(msg.encode(), addr)
            except OSError as exc:
                log.debug("SSDP 响应发送失败 %s: %s", addr, exc)
