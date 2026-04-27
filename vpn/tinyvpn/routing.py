"""Automatic route and DNS management."""

from __future__ import annotations

import platform
import subprocess

from .config import TunConfig


class RouteManager:
    def __init__(self, tun: TunConfig):
        self.tun = tun
        self._cleanup: list[list[str]] = []

    def apply(self) -> None:
        system = platform.system()
        if system == "Windows":
            self._apply_windows()
        elif system == "Linux":
            self._apply_linux()
        elif system == "Darwin":
            self._apply_macos()

    def cleanup(self) -> None:
        for cmd in reversed(self._cleanup):
            self._run(cmd, check=False)

    def _apply_windows(self) -> None:
        alias = self.tun.name
        self._run(
            [
                "netsh",
                "interface",
                "ipv4",
                "set",
                "address",
                f"name={alias}",
                "static",
                self.tun.address,
                self.tun.netmask,
                self.tun.gateway,
            ]
        )
        if self.tun.redirect_default_route:
            self._run(["route", "add", "0.0.0.0", "mask", "0.0.0.0", self.tun.gateway, "metric", "3"])
            self._cleanup.append(["route", "delete", "0.0.0.0"])
        for cidr in self.tun.extra_routes:
            network, prefix = cidr.split("/")
            self._run(["route", "add", network, "mask", _prefix_to_mask(int(prefix)), self.tun.gateway, "metric", "5"])
            self._cleanup.append(["route", "delete", network])
        if self.tun.dns_servers:
            self._run(
                [
                    "netsh",
                    "interface",
                    "ipv4",
                    "set",
                    "dnsservers",
                    f"name={alias}",
                    "static",
                    self.tun.dns_servers[0],
                    "primary",
                ]
            )
            for index, dns_value in enumerate(self.tun.dns_servers[1:], start=2):
                self._run(
                    [
                        "netsh",
                        "interface",
                        "ipv4",
                        "add",
                        "dnsservers",
                        f"name={alias}",
                        dns_value,
                        f"index={index}",
                    ]
                )
            self._cleanup.append(["netsh", "interface", "ipv4", "set", "dnsservers", f"name={alias}", "dhcp"])

    def _apply_linux(self) -> None:
        for cidr in self.tun.extra_routes:
            self._run(["ip", "route", "replace", cidr, "dev", self.tun.name])
            self._cleanup.append(["ip", "route", "del", cidr, "dev", self.tun.name])
        if self.tun.redirect_default_route:
            self._run(["ip", "route", "replace", "default", "via", self.tun.gateway, "dev", self.tun.name])
            self._cleanup.append(["ip", "route", "del", "default", "via", self.tun.gateway, "dev", self.tun.name])

    def _apply_macos(self) -> None:
        for cidr in self.tun.extra_routes:
            self._run(["route", "add", "-net", cidr, self.tun.gateway])
            self._cleanup.append(["route", "delete", "-net", cidr, self.tun.gateway])
        if self.tun.redirect_default_route:
            self._run(["route", "add", "default", self.tun.gateway])
            self._cleanup.append(["route", "delete", "default", self.tun.gateway])

    def _run(self, cmd: list[str], *, check: bool = True) -> None:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if check and result.returncode != 0:
            raise RuntimeError(f"Command failed: {' '.join(cmd)} :: {result.stderr.strip()}")


def _prefix_to_mask(prefix: int) -> str:
    mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
    return ".".join(str((mask >> offset) & 0xFF) for offset in (24, 16, 8, 0))
