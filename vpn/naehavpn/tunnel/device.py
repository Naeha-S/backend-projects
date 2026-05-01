"""Portable TUN/Wintun wrapper for NaehaVPN."""

from __future__ import annotations

import os
import platform
import struct
import subprocess
import sys

TUNSETIFF = 0x400454CA
IFF_TUN = 0x0001
IFF_NO_PI = 0x1000


class TunDevice:
    mtu: int

    def read(self, size: int) -> bytes:
        raise NotImplementedError

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class PosixTunDevice(TunDevice):
    def __init__(self, fd: int, mtu: int):
        self.fd = fd
        self.mtu = mtu
        self.closed = False

    def read(self, size: int) -> bytes:
        data = os.read(self.fd, size)
        if len(data) > self.mtu:
            raise OSError(f"TUN packet exceeded MTU ({len(data)} > {self.mtu})")
        return data

    def write(self, data: bytes) -> None:
        if len(data) > self.mtu:
            raise OSError(f"TUN packet exceeded MTU ({len(data)} > {self.mtu})")
        os.write(self.fd, data)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            os.close(self.fd)


class WindowsTunDevice(TunDevice):
    def __init__(self, device, mtu: int):
        self.device = device
        self.mtu = mtu
        self.closed = False

    def read(self, size: int) -> bytes:
        if self.closed:
            raise OSError("tun device is closed")
        data = self.device.read(size)
        if len(data) > self.mtu:
            raise OSError(f"TUN packet exceeded MTU ({len(data)} > {self.mtu})")
        return data

    def write(self, data: bytes) -> None:
        if self.closed:
            raise OSError("tun device is closed")
        if len(data) > self.mtu:
            raise OSError(f"TUN packet exceeded MTU ({len(data)} > {self.mtu})")
        self.device.write(data)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.device.down()
        except Exception:
            pass
        try:
            self.device.close()
        except Exception:
            pass


def create_tun(name: str, address: str, netmask: str, mtu: int) -> TunDevice:
    system = platform.system()
    if system == "Windows":
        try:
            import pytun_pmd3
        except ImportError as exc:
            raise RuntimeError("pytun-pmd3 is required on Windows") from exc
        dev = pytun_pmd3.TunTapDevice(name=name)
        dev.mtu = mtu
        try:
            dev.addr = address
            dev.netmask = netmask
        except AttributeError:
            _run(
                [
                    "netsh",
                    "interface",
                    "ipv4",
                    "set",
                    "address",
                    f"name={name}",
                    "static",
                    address,
                    netmask,
                ]
            )
        dev.up()
        return WindowsTunDevice(dev, mtu)

    if system == "Linux":
        import fcntl

        fd = os.open("/dev/net/tun", os.O_RDWR)
        ifr = struct.pack("16sH14s", name.encode("ascii"), IFF_TUN | IFF_NO_PI, b"\x00" * 14)
        fcntl.ioctl(fd, TUNSETIFF, ifr)
        _run(["ip", "addr", "replace", f"{address}/24", "dev", name])
        _run(["ip", "link", "set", name, "mtu", str(mtu)])
        _run(["ip", "link", "set", name, "up"])
        return PosixTunDevice(fd, mtu)

    if system == "Darwin":
        dev_path = f"/dev/{name}"
        if not os.path.exists(dev_path):
            raise RuntimeError(f"{dev_path} not found")
        fd = os.open(dev_path, os.O_RDWR)
        _run(["ifconfig", name, address, "10.44.0.1", "up"])
        _run(["ifconfig", name, "mtu", str(mtu)])
        return PosixTunDevice(fd, mtu)

    raise RuntimeError(f"Unsupported platform: {system}")


def require_admin() -> None:
    if platform.system() == "Windows":
        import ctypes

        if ctypes.windll.shell32.IsUserAnAdmin() == 0:
            sys.exit("[!] NaehaVPN must be run as Administrator")
        return
    if os.geteuid() != 0:
        sys.exit("[!] NaehaVPN must be run as root")


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)} :: {result.stderr.strip()}")
