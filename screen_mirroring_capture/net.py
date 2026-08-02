"""LAN IP auto-detection, skipping VPN/proxy virtual interfaces."""

from __future__ import annotations

import socket

import ifaddr

# Only consider physical network interfaces (Ethernet / WiFi).
# Virtual interfaces (Tailscale utun*, VPN tun*, Docker veth*, etc.) are skipped.
_PHYSICAL_PREFIXES = ("en", "eth", "wlan")
_PHYSICAL_SUBSTRINGS = ("wlan", "wi-fi", "wifi", "wireless", "以太网", "无线")
_VIRTUAL_KEYWORDS = ("vmware", "virtualbox", "hyper-v", "vmnet", "docker",
                     "tailscale", "utun", "tun", "veth", "virtual",
                     "radmin", "pseudo")


def _get_display_name(adapter: ifaddr.Adapter) -> str:
    """Return the human-readable name (preferring nice_name on Windows)."""
    nice = getattr(adapter, "nice_name", None)
    return nice if nice else adapter.name


def _search_names(adapter: ifaddr.Adapter) -> tuple[str, str]:
    """Return (lowercase_name, lowercase_nice_name) for keyword searching."""
    name = adapter.name.lower()
    nice = getattr(adapter, "nice_name", "").lower()
    return name, nice


def _is_virtual(adapter: ifaddr.Adapter) -> bool:
    name, nice = _search_names(adapter)
    return any(kw in name or kw in nice for kw in _VIRTUAL_KEYWORDS)


def _is_physical(adapter: ifaddr.Adapter) -> bool:
    name, nice = _search_names(adapter)
    if name.startswith(_PHYSICAL_PREFIXES) or nice.startswith(_PHYSICAL_PREFIXES):
        return True
    return any(sub in name or sub in nice for sub in _PHYSICAL_SUBSTRINGS)


def _is_private(ip: str) -> bool:
    if ip.startswith(("192.168.", "10.")):
        return True
    if ip.startswith("172."):
        octet2 = int(ip.split(".")[1])
        return 16 <= octet2 <= 31
    return False


def get_lan_ip() -> str:
    """Return the first private LAN IP found on a physical network interface.

    Only considers ``en*`` (macOS Ethernet/WiFi), ``eth*`` and ``wlan*``
    (Linux).  Skips VPN (utun/tun), Tailscale, Docker, and other virtual
    interfaces.

    Falls back to the default-route IP if no match is found.
    """
    adapters = ifaddr.get_adapters()

    # Pass 1: physical interfaces only.
    for adapter in adapters:
        if not _is_physical(adapter):
            continue
        for ip_info in adapter.ips:
            if not isinstance(ip_info.ip, str):
                continue
            if _is_private(ip_info.ip):
                return ip_info.ip

    # Pass 2: any non-virtual interface (in case naming doesn't match).
    for adapter in adapters:
        if _is_virtual(adapter):
            continue
        for ip_info in adapter.ips:
            if not isinstance(ip_info.ip, str):
                continue
            if ip_info.ip.startswith("127."):
                continue
            if _is_private(ip_info.ip):
                return ip_info.ip

    # Fallback: default route (may hit VPN).
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def resolve_bind(val: str) -> str:
    """Resolve a bind target to an IP address.

    Accepts either an IP address ("192.168.1.100") or an interface name ("en1").
    """
    # If it looks like an IP, use directly.
    try:
        socket.inet_aton(val)
        return val
    except OSError:
        pass

    # Otherwise treat as interface name.
    for adapter in ifaddr.get_adapters():
        if adapter.name == val:
            for ip_info in adapter.ips:
                if isinstance(ip_info.ip, str) and not ip_info.ip.startswith("127."):
                    return ip_info.ip
    raise RuntimeError(f"no IPv4 address found for interface '{val}'")


def list_adapters() -> list[tuple[str, str]]:
    """Return (ip, display_name) for each non-loopback adapter.

    Only adapters with a private IPv4 address are included.
    """
    results: list[tuple[str, str]] = []
    for adapter in ifaddr.get_adapters():
        for ip_info in adapter.ips:
            if not isinstance(ip_info.ip, str):
                continue
            if not _is_private(ip_info.ip):
                continue
            results.append((ip_info.ip, _get_display_name(adapter)))
    return results
