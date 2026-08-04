"""screen-mirroring-capture — Capture live stream URLs via fake screen casting.

Pretends to be a TV on your local network. When a live stream is cast
to it, the real m3u8 stream URL is captured.

Supports multiple casting protocols: DLNA/UPnP, AirPlay, and Google Cast.

Example::

    from screen_mirroring_capture import capture

    url = capture(name="My TV")
    print(f"Stream: {url}")

    # AirPlay only
    url = capture(name="My TV", protocols=["airplay"])

    # All protocols
    url = capture(name="My TV", protocols=["dlna", "airplay", "cast"])
"""

from __future__ import annotations

import logging
import sys
import threading
import uuid
from http.server import HTTPServer
from typing import Callable

log = logging.getLogger(__name__)

from .net import get_lan_ip  # noqa: E402
from .ssdp import SSDPAdvertiser  # noqa: E402
from .upnp import UPnPHandler  # noqa: E402

__all__ = ["capture"]

PROTOCOLS = ("dlna", "airplay", "cast")


def capture(
    *,
    name: str = "MAGI",
    port: int = 9090,
    airplay_port: int | None = None,
    cast_port: int = 8009,
    on_url: Callable[[str], None] | None = None,
    on_play: Callable[[], None] | None = None,
    protocols: list[str] | None = None,
    audio_output: str | None = None,
    audio_duration: float | None = None,
    stop_event: threading.Event | None = None,
    bind_ip: str | None = None,
    continuous: bool = False,
) -> str | None:
    """Start fake casting receivers and block until a URL is captured.

    Args:
        name: Device name shown in the cast list.
        port: DLNA HTTP port (default: 9090).
        airplay_port: AirPlay port. If None, uses port+1 when DLNA is enabled, otherwise uses port.
        cast_port: Google Cast port (default: 8009).
        on_url: Optional callback fired when a URL is captured.
        on_play: Optional callback fired when a Play command is received.
        protocols: List of protocols to enable. Defaults to all:
                   ``["dlna", "airplay", "cast"]``.
        audio_output: Directory to save AirPlay audio capture.
        audio_duration: Max audio capture duration in seconds.
        stop_event: Optional threading.Event. When set, the function
                    stops waiting and returns ``None``.
        bind_ip: Optional IP address to bind services to. When set,
                 overrides auto-detection via ``get_lan_ip()``.
        continuous: If True, keep waiting after each URL capture until
                    stop_event is set. Calls on_url for each URL.

    Returns:
        The captured stream/video URL, or ``None`` if stopped early.
    """
    if protocols is None:
        protocols = list(PROTOCOLS)
    for p in protocols:
        if p not in PROTOCOLS:
            raise ValueError(f"Unknown protocol {p!r}, expected one of {PROTOCOLS}")

    local_ip = bind_ip if bind_ip else get_lan_ip()
    dev_uuid = f"uuid:{uuid.uuid4()}"

    result: list[str] = []
    event = threading.Event()

    def _handle(url: str) -> None:
        if not continuous and result:
            return
        if not continuous:
            result.append(url)
        if on_url:
            on_url(url)
        event.set()

    cleanups: list[Callable[[], None]] = []
    started: list[str] = []

    if "dlna" in protocols:
        try:
            location = f"http://{local_ip}:{port}/device.xml"
            UPnPHandler.device_uuid = dev_uuid
            UPnPHandler.friendly_name = name
            UPnPHandler.on_url = staticmethod(_handle)
            UPnPHandler.on_play = staticmethod(on_play)
            UPnPHandler._captured = False

            server = HTTPServer(("", port), UPnPHandler)
            ssdp = SSDPAdvertiser(dev_uuid, location, local_ip)
            ssdp.start()
            threading.Thread(target=server.serve_forever, daemon=True).start()
            cleanups.extend([server.shutdown, ssdp.stop])
            started.append("dlna")
            log.debug("DLNA advertised on %s:%d", local_ip, port)
            print(f'  📺 DLNA    "{name}" on {local_ip}:{port}', file=sys.stderr)
        except Exception:
            log.warning("Failed to start DLNA", exc_info=True)
            print("  ⚠️  DLNA   failed to start (see --verbose)", file=sys.stderr)

    if "airplay" in protocols:
        try:
            from .airplay import AirPlayReceiver

            ap_port = airplay_port if airplay_port is not None else (port + 1 if "dlna" in protocols else port)
            airplay_recv = AirPlayReceiver(
                name,
                local_ip,
                ap_port,
                _handle,
                on_play=on_play,
                audio_output=audio_output,
                audio_duration=audio_duration,
            )
            airplay_recv.start()
            cleanups.append(airplay_recv.stop)
            started.append("airplay")
            print(
                f'  🍎 AirPlay "{name}" on {local_ip}:{ap_port}', file=sys.stderr
            )
        except Exception:
            log.warning("Failed to start AirPlay", exc_info=True)
            print("  ⚠️  AirPlay failed to start (see --verbose)", file=sys.stderr)

    if "cast" in protocols:
        try:
            from .cast import CastReceiver

            cp_port = cast_port
            cast_recv = CastReceiver(name, local_ip, cp_port, _handle, on_play=on_play)
            cast_recv.start()
            cleanups.append(cast_recv.stop)
            started.append("cast")
            print(f'  📡 Cast    "{name}" on {local_ip}:{cp_port}', file=sys.stderr)
        except Exception:
            log.warning("Failed to start Cast", exc_info=True)
            print("  ⚠️  Cast   failed to start (see --verbose)", file=sys.stderr)

    if not started:
        raise RuntimeError("All protocols failed to start")

    enabled = ", ".join(p.upper() for p in started)
    print(f"\n  Protocols: {enabled}", file=sys.stderr)
    print(f'  Open your app > cast > select "{name}"\n', file=sys.stderr)

    try:
        if stop_event is not None:
            while not stop_event.is_set():
                if event.wait(timeout=0.2):
                    if continuous:
                        event.clear()
                    else:
                        break
        else:
            event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        for fn in cleanups:
            try:
                fn()
            except Exception:
                pass

    if not result:
        return None
    return result[0] if not continuous else (result[-1] if result else None)
