"""Automatic route and DNS management with endpoint exclusions."""

from __future__ import annotations

import ipaddress
import platform
import re
import socket
import subprocess
from dataclasses import dataclass

from ..config import PeerConfig, TunConfig


@dataclass(slots=True)
class RouteTarget:
    address: str
    gateway: str
    interface: str | None = None


class RouteManager:
    def __init__(self, tun: TunConfig, peers: list[PeerConfig]):
        self.tun = tun
        self.peers = peers
        self._cleanup: list[list[str]] = []

    def apply(self) -> None:
        system = platform.system()
        endpoint_routes = self._resolve_endpoint_routes()
        if system == "Windows":
            self._apply_windows(endpoint_routes)
        elif system == "Linux":
            self._apply_linux(endpoint_routes)
        elif system == "Darwin":
            self._apply_macos(endpoint_routes)

    def cleanup(self) -> None:
        for cmd in reversed(self._cleanup):
            self._run(cmd, check=False)

    def _resolve_endpoint_routes(self) -> list[RouteTarget]:
        if not self.tun.redirect_default_route:
            return []
        default_route = self._discover_default_route()
        if default_route is None:
            return []
        routes: list[RouteTarget] = []
        for peer in self.peers:
            if not peer.host:
                continue
            try:
                address = socket.gethostbyname(peer.host)
                ipaddress.ip_address(address)
            except Exception:
                continue
            routes.append(RouteTarget(address=address, gateway=default_route.gateway, interface=default_route.interface))
        return routes

    def _discover_default_route(self) -> RouteTarget | None:
        system = platform.system()
        if system == "Windows":
            result = subprocess.run(["route", "print", "0.0.0.0"], capture_output=True, text=True)
            if result.returncode != 0:
                return None
            for line in result.stdout.splitlines():
                match = re.search(r"^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\S+)\s+(\S+)\s+\d+\s*$", line)
                if match:
                    return RouteTarget(address="0.0.0.0", gateway=match.group(1), interface=match.group(2))
            return None
        if system == "Linux":
            result = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True)
            if result.returncode != 0:
                return None
            match = re.search(r"default via (\S+) dev (\S+)", result.stdout)
            if match:
                return RouteTarget(address="0.0.0.0", gateway=match.group(1), interface=match.group(2))
            return None
        if system == "Darwin":
            result = subprocess.run(["route", "-n", "get", "default"], capture_output=True, text=True)
            if result.returncode != 0:
                return None
            gateway = None
            interface = None
            for line in result.stdout.splitlines():
                if "gateway:" in line:
                    gateway = line.split(":", 1)[1].strip()
                if "interface:" in line:
                    interface = line.split(":", 1)[1].strip()
            if gateway:
                return RouteTarget(address="0.0.0.0", gateway=gateway, interface=interface)
        return None

    def _apply_windows(self, endpoint_routes: list[RouteTarget]) -> None:
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
        for target in endpoint_routes:
            self._run(["route", "add", target.address, "mask", "255.255.255.255", target.gateway, "metric", "1"])
            self._cleanup.append(["route", "delete", target.address])
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

    def _apply_linux(self, endpoint_routes: list[RouteTarget]) -> None:
        for target in endpoint_routes:
            self._run(["ip", "route", "replace", target.address, "via", target.gateway, "dev", target.interface or ""])
            self._cleanup.append(["ip", "route", "del", target.address, "via", target.gateway])
        for cidr in self.tun.extra_routes:
            self._run(["ip", "route", "replace", cidr, "dev", self.tun.name])
            self._cleanup.append(["ip", "route", "del", cidr, "dev", self.tun.name])
        if self.tun.redirect_default_route:
            self._run(["ip", "route", "replace", "default", "via", self.tun.gateway, "dev", self.tun.name])
            self._cleanup.append(["ip", "route", "del", "default", "via", self.tun.gateway, "dev", self.tun.name])

    def _apply_macos(self, endpoint_routes: list[RouteTarget]) -> None:
        for target in endpoint_routes:
            self._run(["route", "add", "-host", target.address, target.gateway])
            self._cleanup.append(["route", "delete", "-host", target.address, target.gateway])
        for cidr in self.tun.extra_routes:
            self._run(["route", "add", "-net", cidr, self.tun.gateway])
            self._cleanup.append(["route", "delete", "-net", cidr, self.tun.gateway])
        if self.tun.redirect_default_route:
            self._run(["route", "add", "default", self.tun.gateway])
            self._cleanup.append(["route", "delete", "default", self.tun.gateway])

    def _run(self, cmd: list[str], *, check: bool = True) -> None:
        cmd = [part for part in cmd if part]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if check and result.returncode != 0:
            raise RuntimeError(f"Command failed: {' '.join(cmd)} :: {result.stderr.strip()}")


def _prefix_to_mask(prefix: int) -> str:
    mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
    return ".".join(str((mask >> offset) & 0xFF) for offset in (24, 16, 8, 0))
