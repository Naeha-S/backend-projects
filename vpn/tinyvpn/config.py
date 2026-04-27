"""Configuration loading for tinyvpn."""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class DashboardConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8080
    interval_seconds: float = 1.0


@dataclass(slots=True)
class DebugConfig:
    dpi: bool = False
    log_level: str = "INFO"


@dataclass(slots=True)
class TunConfig:
    name: str
    address: str
    netmask: str = "255.255.255.0"
    gateway: str = "10.44.0.1"
    mtu: int = 1360
    dns_servers: list[str] = field(default_factory=list)
    redirect_default_route: bool = False
    extra_routes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PeerConfig:
    name: str
    public_key_file: str
    host: str | None = None
    port: int | None = None
    persistent_keepalive: int = 15
    advertised_tun_ip: str | None = None


@dataclass(slots=True)
class NodeConfig:
    role: str
    node_name: str
    private_key_file: str
    listen_host: str = "0.0.0.0"
    listen_port: int = 8888
    tun: TunConfig | None = None
    peers: list[PeerConfig] = field(default_factory=list)
    route_chain: list[str] = field(default_factory=list)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    debug: DebugConfig = field(default_factory=DebugConfig)
    network_name: str = "tinyvpn-v3"
    handshake_timeout: float = 5.0
    reconnect_max_delay: float = 30.0
    session_max_seconds: int = 180
    session_max_bytes: int = 1_000_000_000
    enable_plugins: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if self.role not in {"client", "server"}:
            raise ValueError(f"Invalid role: {self.role}")
        if self.tun is None:
            raise ValueError("tun configuration is required")
        ipaddress.ip_address(self.tun.address)
        ipaddress.ip_address(self.tun.gateway)
        peer_names = {peer.name for peer in self.peers}
        for hop in self.route_chain:
            if hop not in peer_names:
                raise ValueError(f"route_chain references unknown peer: {hop}")


def load_config(path: str | Path) -> NodeConfig:
    raw_path = Path(path).resolve()
    base = raw_path.parent
    raw = json.loads(raw_path.read_text(encoding="utf-8"))

    def resolve(value: str) -> str:
        candidate = Path(value)
        return str(candidate if candidate.is_absolute() else (base / candidate).resolve())

    peers = []
    for item in raw.get("peers", []):
        entry = dict(item)
        entry["public_key_file"] = resolve(entry["public_key_file"])
        peers.append(PeerConfig(**entry))

    cfg = NodeConfig(
        role=raw["role"],
        node_name=raw["node_name"],
        private_key_file=resolve(raw["private_key_file"]),
        listen_host=raw.get("listen_host", "0.0.0.0"),
        listen_port=raw.get("listen_port", 8888),
        tun=TunConfig(**raw["tun"]),
        peers=peers,
        route_chain=raw.get("route_chain", []),
        dashboard=DashboardConfig(**raw.get("dashboard", {})),
        debug=DebugConfig(**raw.get("debug", {})),
        network_name=raw.get("network_name", "tinyvpn-v3"),
        handshake_timeout=raw.get("handshake_timeout", 5.0),
        reconnect_max_delay=raw.get("reconnect_max_delay", 30.0),
        session_max_seconds=raw.get("session_max_seconds", 180),
        session_max_bytes=raw.get("session_max_bytes", 1_000_000_000),
        enable_plugins=raw.get("enable_plugins", []),
    )
    cfg.validate()
    return cfg
