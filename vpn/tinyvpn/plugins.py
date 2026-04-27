"""Plugin system for send/receive and connection hooks."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass


@dataclass(slots=True)
class PacketContext:
    peer_id: str
    direction: str
    control: bool = False


class Plugin:
    name = "plugin"

    def on_packet_send(self, packet: bytes, context: PacketContext) -> bytes | None:
        return packet

    def on_packet_receive(self, packet: bytes, context: PacketContext) -> bytes | None:
        return packet

    def on_event(self, event: str, **payload: object) -> None:
        return None


class LoggingPlugin(Plugin):
    name = "logging"

    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def on_event(self, event: str, **payload: object) -> None:
        self.logger.debug("plugin event=%s payload=%s", event, payload)


class PacketFilterPlugin(Plugin):
    name = "packet-filter"

    def __init__(self, denied_prefixes: list[bytes] | None = None):
        self.denied_prefixes = denied_prefixes or []

    def on_packet_send(self, packet: bytes, context: PacketContext) -> bytes | None:
        for prefix in self.denied_prefixes:
            if packet.startswith(prefix):
                return None
        return packet


class TrafficShapingPlugin(Plugin):
    name = "traffic-shaper"

    def __init__(self, delay_ms: float = 0.0):
        self.delay_ms = delay_ms

    def on_packet_send(self, packet: bytes, context: PacketContext) -> bytes | None:
        if self.delay_ms:
            time.sleep(self.delay_ms / 1000.0)
        return packet


class PluginManager:
    def __init__(self, plugins: list[Plugin] | None = None):
        self.plugins = plugins or []

    def apply_send(self, packet: bytes, context: PacketContext) -> bytes | None:
        current = packet
        for plugin in self.plugins:
            if current is None:
                return None
            current = plugin.on_packet_send(current, context)
        return current

    def apply_receive(self, packet: bytes, context: PacketContext) -> bytes | None:
        current = packet
        for plugin in self.plugins:
            if current is None:
                return None
            current = plugin.on_packet_receive(current, context)
        return current

    def emit(self, event: str, **payload: object) -> None:
        for plugin in self.plugins:
            plugin.on_event(event, **payload)
