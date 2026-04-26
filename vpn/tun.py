"""
tun.py — Portable TUN device helper for tinyvpn
Supports Linux and macOS (Darwin).
Must be run as root.
"""

import os
import fcntl
import struct
import subprocess
import platform
import sys

# ── Linux ioctl constants ──────────────────────────────────────────────────────
TUNSETIFF = 0x400454CA
IFF_TUN   = 0x0001
IFF_NO_PI = 0x1000   # strip 4-byte packet-info header; gives us raw IP


def open_tun(name: str = "tun0") -> int:
    """
    Open a TUN virtual network interface and return its file descriptor.

    On Linux  : opens /dev/net/tun and binds the named interface via ioctl.
    On macOS  : opens /dev/tunN directly (utun requires different API; use tun).

    The returned fd behaves like a regular file:
        os.read(fd, 65535)  → one raw IP packet (no Ethernet header)
        os.write(fd, pkt)   → inject one raw IP packet into the kernel stack
    """
    system = platform.system()

    if system == "Linux":
        fd = os.open("/dev/net/tun", os.O_RDWR)
        # ifreq struct: 16-byte name + 2-byte flags, rest padding
        ifr = struct.pack("16sH14s", name.encode(), IFF_TUN | IFF_NO_PI, b"\x00" * 14)
        fcntl.ioctl(fd, TUNSETIFF, ifr)
        return fd

    elif system == "Darwin":
        # macOS ships /dev/tun0 … /dev/tun15 via the utun kernel extension.
        # Numbering: tun0 → /dev/tun0
        dev = f"/dev/{name}"
        if not os.path.exists(dev):
            sys.exit(
                f"[!] {dev} not found.\n"
                "    Install TunTap: https://github.com/Tunnelblick/Tunnelblick"
                " or use `brew install --cask tuntap`"
            )
        fd = os.open(dev, os.O_RDWR)
        return fd

    else:
        sys.exit(f"[!] Unsupported platform: {system}")


def configure_iface(name: str, local_ip: str, peer_ip: str | None = None):
    """
    Assign an IP address to the TUN interface and bring it up.

    Args:
        name      : interface name, e.g. "tun0"
        local_ip  : IP to assign, e.g. "10.0.0.1"
        peer_ip   : optional point-to-point peer IP (macOS needs this)
    """
    system = platform.system()
    peer   = peer_ip or _peer_for(local_ip)

    if system == "Linux":
        _run(["ip", "addr", "add", f"{local_ip}/24", "dev", name])
        _run(["ip", "link", "set", name, "up"])

    elif system == "Darwin":
        # macOS ifconfig syntax: ifconfig tunN <local> <peer> up
        _run(["ifconfig", name, local_ip, peer, "up"])
        _run(["route", "add", "-net", "10.0.0.0/24", local_ip])

    print(f"[*] {name} up — {local_ip}/24  (peer hint {peer})")


# ── helpers ───────────────────────────────────────────────────────────────────

def _run(cmd: list[str]):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[!] Command failed: {' '.join(cmd)}")
        print(f"    stderr: {result.stderr.strip()}")
        sys.exit(1)


def _peer_for(ip: str) -> str:
    """Guess a sensible peer IP (flip last octet between .1 and .2)."""
    parts = ip.split(".")
    parts[-1] = "2" if parts[-1] == "1" else "1"
    return ".".join(parts)
