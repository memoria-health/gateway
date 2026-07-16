#!/usr/bin/env python3
"""Camera discovery — find RTSP cameras on the local network.

Dependency-free subnet sweep for TCP 554 (RTSP), which is reliable on a flat LAN
— exactly what the gateway's own mesh (or a facility's camera VLAN) is. Tapo also
exposes ONVIF on 2020, which we flag, but RTSP/554 is what we actually pull.

This replaces hand-typing camera IPs in the installer flow: plug in the mini-PC,
hit "Discover", and the Tapos show up by themselves.
"""
import asyncio
import re
import socket
import subprocess


def _normalize_mac(mac):
    """Zero-pad + lowercase so 'a:b:1f:...' and '0A:0B:1F:...' compare equal."""
    parts = mac.split(":")
    if len(parts) != 6:
        return None
    try:
        return ":".join(f"{int(p, 16):02x}" for p in parts)
    except ValueError:
        return None


def _mac_for_ip(ip):
    """The camera's MAC from the neighbor/ARP table (populated by the port
    probe just before). Stable across IP changes — the anchor for
    re-discovery. `ip neigh` first: the gateway's Ubuntu image ships without
    `arp` (net-tools), which silently left every provisioned camera with
    mac=null and broke relocation. `arp -n` is the macOS fallback (no `ip`
    there). A FAILED neighbor entry has no lladdr, so it falls through."""
    for cmd in (["ip", "neigh", "show", ip], ["arp", "-n", ip]):
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True, timeout=3).stdout
        except Exception:  # noqa: BLE001
            continue
        m = re.search(r"([0-9a-fA-F]{1,2}(?::[0-9a-fA-F]{1,2}){5})", out)
        if m:
            return _normalize_mac(m.group(1))
    return None


def local_subnet():
    """Best-effort /24 the gateway sits on, e.g. '192.168.2'. Opens a UDP socket
    to a public IP to learn our primary local address (sends nothing)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    finally:
        s.close()
    return ".".join(ip.split(".")[:3])


async def _probe_port(ip, port, timeout):
    try:
        fut = asyncio.open_connection(ip, port)
        _, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return True
    except Exception:  # noqa: BLE001
        return False


async def _check_host(prefix, n, timeout):
    ip = f"{prefix}.{n}"
    if not await _probe_port(ip, 554, timeout):
        return None
    onvif = await _probe_port(ip, 2020, timeout)   # Tapo ONVIF port
    mac = await asyncio.to_thread(_mac_for_ip, ip)
    return {"ip": ip, "rtsp_port": 554, "onvif": onvif, "mac": mac}


async def discover(prefix=None, timeout=0.6):
    """Scan the /24 for open RTSP ports. Returns [{ip, rtsp_port, onvif}, ...]."""
    prefix = prefix or local_subnet()
    results = await asyncio.gather(
        *[_check_host(prefix, n, timeout) for n in range(1, 255)])
    return [r for r in results if r]


if __name__ == "__main__":
    for c in asyncio.run(discover()):
        print(c)
