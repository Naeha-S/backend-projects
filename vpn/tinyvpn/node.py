"""Runtime node for tinyvpn."""

from __future__ import annotations

import argparse
import logging
import socket
import threading
import time
from dataclasses import dataclass

from .config import NodeConfig, PeerConfig, load_config
from .congestion import AdaptivePacer
from .crypto import (
    HANDSHAKE_INIT,
    HANDSHAKE_REPLY,
    ReplayWindow,
    SecureChannel,
    SessionMaterial,
    build_handshake_init,
    complete_handshake,
    fingerprint,
    load_private_key,
    load_public_key,
    public_key_bytes,
    respond_handshake_init,
)
from .dashboard import DashboardServer
from .dpi import decode_packet
from .fragmentation import Fragmenter, Reassembler
from .metrics import MetricsRegistry
from .plugins import LoggingPlugin, PacketContext, PacketFilterPlugin, PluginManager, TrafficShapingPlugin
from .protocol import (
    CONTROL_KEEPALIVE,
    CONTROL_KEEPALIVE_ACK,
    HEADER_LEN,
    MSG_CONTROL,
    MSG_DATA,
    PacketHeader,
    VERSION,
    build_header,
    decode_control,
    decode_route_layer,
    encode_keepalive,
    encode_keepalive_ack,
    encode_route_layer,
)
from .routing import RouteManager
from .tun import TunDevice, create_tun


@dataclass(slots=True)
class PendingHandshake:
    peer_name: str
    address: tuple[str, int]
    state: object
    created_at: float


class PeerState:
    def __init__(self, config: PeerConfig | None, material: SessionMaterial, address: tuple[str, int], mtu: int):
        self.config = config
        self.peer_id = fingerprint(material.peer_static)
        self.material = material
        self.address = address
        self.virtual_ip = material.peer_virtual_ip
        self.keepalive_seconds = material.keepalive_seconds
        self.send_channel = SecureChannel(material.send_key)
        self.recv_channel = SecureChannel(material.recv_key)
        self.replay = ReplayWindow()
        self.fragmenter = Fragmenter(max(256, mtu - 160))
        self.reassembler = Reassembler()
        self.pacer = AdaptivePacer()
        self.last_seen = time.time()
        self.last_send = 0.0
        self.lock = threading.Lock()

    def needs_rekey(self, max_seconds: int, max_bytes: int) -> bool:
        return (
            self.send_channel.age_seconds >= max_seconds
            or self.send_channel.bytes_encrypted >= max_bytes
        )


class VpnNode:
    def __init__(self, config: NodeConfig):
        self.config = config
        self.log = logging.getLogger("tinyvpn")
        self.private_key = load_private_key(config.private_key_file)
        self.metrics = MetricsRegistry()
        self.metrics.set_connection_mode(self._describe_connection_mode())
        self.plugins = self._build_plugins()
        self.route_manager = RouteManager(config.tun)
        self.pending: dict[int, PendingHandshake] = {}
        self.peers_by_id: dict[str, PeerState] = {}
        self.peers_by_tunnel: dict[int, PeerState] = {}
        self.peers_by_ip: dict[str, PeerState] = {}
        self.peer_configs = {peer.name: peer for peer in config.peers}
        self.allowed_peer_ids = self._load_allowed_peer_ids()
        self.running = threading.Event()
        self.tun: TunDevice | None = None
        self.sock: socket.socket | None = None
        self.dashboard: DashboardServer | None = None
        self._connect_lock = threading.Lock()

    def _describe_connection_mode(self) -> str:
        if self.config.role == "client":
            if len(self.config.route_chain) > 1:
                return f"client-multihop-{len(self.config.route_chain)}"
            return "client-single-hop"
        if len(self.config.peers) > 1:
            return "server-multi-peer"
        return "server-single-peer"

    def _build_plugins(self) -> PluginManager:
        plugins = []
        for name in self.config.enable_plugins:
            if name == "logging":
                plugins.append(LoggingPlugin(self.log))
            elif name == "packet-filter":
                plugins.append(PacketFilterPlugin())
            elif name == "traffic-shaper":
                plugins.append(TrafficShapingPlugin(delay_ms=2.0))
        return PluginManager(plugins)

    def _load_allowed_peer_ids(self) -> dict[str, PeerConfig]:
        out: dict[str, PeerConfig] = {}
        for peer in self.config.peers:
            peer_id = fingerprint(public_key_bytes(load_public_key(peer.public_key_file)))
            out[peer_id] = peer
        return out

    def start(self) -> None:
        self.running.set()
        self.tun = create_tun(
            self.config.tun.name,
            self.config.tun.address,
            self.config.tun.netmask,
            self.config.tun.mtu,
        )
        self.route_manager.apply()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.config.listen_host, self.config.listen_port))
        self.sock.settimeout(1.0)

        if self.config.dashboard.enabled:
            self.dashboard = DashboardServer(
                self.metrics,
                self.config.dashboard.host,
                self.config.dashboard.port,
                self.config.dashboard.interval_seconds,
            )
            self.dashboard.start()

        threading.Thread(target=self._recv_loop, daemon=True, name="udp-recv").start()
        threading.Thread(target=self._keepalive_loop, daemon=True, name="keepalive").start()
        threading.Thread(target=self._stats_loop, daemon=True, name="stats").start()

        if self.config.role == "client":
            self._connect_all()
            threading.Thread(target=self._reconnect_loop, daemon=True, name="reconnect").start()

        self._tun_loop()

    def stop(self) -> None:
        self.running.clear()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        if self.tun:
            try:
                self.tun.close()
            except OSError:
                pass
        try:
            self.route_manager.cleanup()
        except Exception as exc:
            self.log.debug("route cleanup failed: %s", exc)

    def _connect_all(self) -> None:
        for peer in self.config.peers:
            if peer.host and peer.port:
                self._initiate_handshake(peer)

    def _initiate_handshake(self, peer_cfg: PeerConfig) -> None:
        remote_static = load_public_key(peer_cfg.public_key_file)
        state, wire = build_handshake_init(
            self.private_key,
            remote_static,
            self.config.tun.address,
            network_name=self.config.network_name,
            keepalive_seconds=peer_cfg.persistent_keepalive,
        )
        self.pending[state.session_id] = PendingHandshake(
            peer_name=peer_cfg.name,
            address=(peer_cfg.host or "127.0.0.1", int(peer_cfg.port or self.config.listen_port)),
            state=state,
            created_at=time.time(),
        )
        assert self.sock is not None
        self.sock.sendto(wire, (peer_cfg.host, peer_cfg.port))
        self.metrics.inc("handshake_count")

    def _install_peer(self, peer_cfg: PeerConfig | None, material: SessionMaterial, address: tuple[str, int]) -> PeerState:
        existing = self.peers_by_id.get(fingerprint(material.peer_static))
        if existing is not None:
            self.peers_by_tunnel.pop(existing.material.tunnel_id, None)
            if existing.virtual_ip:
                self.peers_by_ip.pop(existing.virtual_ip, None)
        peer = PeerState(peer_cfg, material, address, self.config.tun.mtu)
        self.peers_by_id[peer.peer_id] = peer
        self.peers_by_tunnel[material.tunnel_id] = peer
        if peer.virtual_ip and peer.virtual_ip != "0.0.0.0":
            self.peers_by_ip[peer.virtual_ip] = peer
        if peer_cfg and peer_cfg.advertised_tun_ip:
            self.peers_by_ip[peer_cfg.advertised_tun_ip] = peer
        self.metrics.register_peer(peer.peer_id, self._describe_connection_mode())
        self.metrics.peer_handshake(peer.peer_id)
        self.plugins.emit("peer_up", peer_id=peer.peer_id, address=address)
        return peer

    def _recv_loop(self) -> None:
        assert self.sock is not None
        while self.running.is_set():
            try:
                packet, address = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break

            if not packet:
                continue
            if packet[0] == HANDSHAKE_INIT:
                self._handle_handshake_init(packet, address)
                continue
            if packet[0] == HANDSHAKE_REPLY:
                self._handle_handshake_reply(packet, address)
                continue
            if len(packet) < HEADER_LEN:
                self.metrics.inc("packet_drops")
                continue

            header = PacketHeader.unpack(packet)
            if header.version != VERSION:
                self.metrics.inc("packet_drops")
                continue
            peer = self.peers_by_tunnel.get(header.tunnel_id)
            if peer is None:
                self.metrics.inc("auth_failures")
                continue
            self._handle_encrypted(peer, header, packet[HEADER_LEN:], address)

    def _handle_handshake_init(self, packet: bytes, address: tuple[str, int]) -> None:
        result = respond_handshake_init(
            self.private_key,
            packet,
            network_name=self.config.network_name,
        )
        if result is None:
            self.metrics.inc("auth_failures")
            return
        material, reply = result
        peer_id = fingerprint(material.peer_static)
        peer_cfg = self.allowed_peer_ids.get(peer_id)
        if self.config.role == "server" and self.allowed_peer_ids and peer_cfg is None:
            self.metrics.inc("auth_failures")
            return
        peer = self._install_peer(peer_cfg, material, address)
        assert self.sock is not None
        self.sock.sendto(reply, address)
        self.metrics.inc("handshake_count")
        self.log.info("peer connected peer=%s addr=%s", peer.peer_id, address)
        self._send_control(peer, encode_keepalive(time.monotonic_ns()))

    def _handle_handshake_reply(self, packet: bytes, address: tuple[str, int]) -> None:
        session_id = int.from_bytes(packet[1:5], "big")
        pending = self.pending.pop(session_id, None)
        if pending is None:
            return
        material = complete_handshake(
            pending.state,
            packet,
            network_name=self.config.network_name,
        )
        if material is None:
            self.metrics.inc("auth_failures")
            return
        peer_cfg = self.peer_configs[pending.peer_name]
        if peer_cfg.advertised_tun_ip:
            material.peer_virtual_ip = peer_cfg.advertised_tun_ip
        peer = self._install_peer(peer_cfg, material, address)
        self.log.info("handshake complete peer=%s addr=%s", peer.peer_id, address)
        self._send_control(peer, encode_keepalive(time.monotonic_ns()))

    def _handle_encrypted(self, peer: PeerState, header: PacketHeader, ciphertext: bytes, address: tuple[str, int]) -> None:
        aad = header.pack()
        plaintext = peer.recv_channel.decrypt(header.sequence, ciphertext, aad, peer.replay)
        if plaintext is None:
            self.metrics.inc("auth_failures")
            self.metrics.peer_loss(peer.peer_id)
            self.metrics.peer_error(peer.peer_id, f"decrypt-failed msg_type={header.msg_type} tunnel_id={header.tunnel_id}")
            peer.pacer.record_loss()
            return

        if peer.address != address:
            peer.address = address
        peer.last_seen = time.time()
        self.metrics.peer_rx(peer.peer_id, len(plaintext))

        context = PacketContext(peer_id=peer.peer_id, direction="inbound", control=header.msg_type == MSG_CONTROL)
        plaintext = self.plugins.apply_receive(plaintext, context)
        if plaintext is None:
            return

        payload = peer.reassembler.add(
            header.tunnel_id,
            header.fragment_id,
            header.fragment_index,
            header.fragment_count,
            plaintext,
        )
        if payload is None:
            self.metrics.inc("fragment_rx_pending")
            return
        self.metrics.inc("fragment_reassembled")

        if header.msg_type == MSG_CONTROL:
            self._handle_control(peer, payload)
            return

        try:
            next_peer_id, inner = decode_route_layer(payload)
        except Exception:
            next_peer_id, inner = None, payload
        if next_peer_id:
            self._forward_to_peer(next_peer_id, inner)
            return
        if self.config.debug.dpi:
            self.log.debug("dpi %s", decode_packet(inner))
        assert self.tun is not None
        self.tun.write(inner)

    def _handle_control(self, peer: PeerState, payload: bytes) -> None:
        ctrl_type, value = decode_control(payload)
        if ctrl_type == CONTROL_KEEPALIVE:
            self._send_control(peer, encode_keepalive_ack(value))
        elif ctrl_type == CONTROL_KEEPALIVE_ACK:
            rtt_ms = (time.monotonic_ns() - value) / 1_000_000
            peer.pacer.record_rtt(rtt_ms)
            self.metrics.peer_rtt(peer.peer_id, rtt_ms, peer.pacer.rate_bps)

    def _forward_to_peer(self, peer_id: str, inner_wire: bytes) -> None:
        peer = self.peers_by_id.get(peer_id)
        if peer is None or self.sock is None:
            self.metrics.inc("packet_drops")
            return
        peer.pacer.wait_for_send(len(inner_wire))
        self.sock.sendto(inner_wire, peer.address)
        self.metrics.peer_tx(peer.peer_id, len(inner_wire))

    def _build_wires(self, peer: PeerState, payload: bytes, msg_type: int) -> list[bytes]:
        wires = []
        for fragment in peer.fragmenter.fragment(payload):
            sequence = peer.send_channel.next_sequence()
            header = build_header(
                msg_type=msg_type,
                tunnel_id=peer.material.tunnel_id,
                sequence=sequence,
                fragment_id=fragment.fragment_id,
                fragment_index=fragment.index,
                fragment_count=fragment.count,
            )
            aad = header.pack()
            ciphertext = peer.send_channel.encrypt(sequence, fragment.payload, aad)
            wires.append(aad + ciphertext)
        return wires

    def _send_payload(self, peer: PeerState, payload: bytes, msg_type: int) -> None:
        assert self.sock is not None
        for wire in self._build_wires(peer, payload, msg_type):
            peer.pacer.wait_for_send(len(wire))
            self.sock.sendto(wire, peer.address)
            peer.last_send = time.time()
            self.metrics.peer_tx(peer.peer_id, len(wire))

    def _send_control(self, peer: PeerState, payload: bytes) -> None:
        self._send_payload(peer, payload, MSG_CONTROL)

    def _select_outbound_peer(self, packet: bytes) -> PeerState | None:
        if self.config.role == "client":
            first_hop = self.config.route_chain[0] if self.config.route_chain else (self.config.peers[0].name if self.config.peers else None)
            if not first_hop:
                return None
            return self.peers_by_id.get(self._peer_id_for_name(first_hop))
        if len(packet) >= 20:
            dst_ip = ".".join(str(part) for part in packet[16:20])
            return self.peers_by_ip.get(dst_ip)
        return None

    def _peer_id_for_name(self, name: str) -> str:
        peer_cfg = self.peer_configs[name]
        return fingerprint(public_key_bytes(load_public_key(peer_cfg.public_key_file)))

    def _build_multihop_payload(self, packet: bytes) -> bytes:
        if len(self.config.route_chain) < 2:
            return packet
        inner = packet
        next_peer_id: str | None = None
        for hop_name in reversed(self.config.route_chain[1:]):
            peer = self.peers_by_id.get(self._peer_id_for_name(hop_name))
            if peer is None:
                raise RuntimeError(f"Route hop {hop_name} is not connected")
            route_layer = encode_route_layer(next_peer_id, inner)
            wires = self._build_wires(peer, route_layer, MSG_DATA)
            if len(wires) != 1:
                raise RuntimeError("Multi-hop encapsulation exceeded one packet; lower the tunnel MTU")
            inner = wires[0]
            next_peer_id = peer.peer_id
        return encode_route_layer(next_peer_id, inner)

    def _tun_loop(self) -> None:
        assert self.tun is not None
        while self.running.is_set():
            packet = self.tun.read(65535)
            if not self.peers_by_id:
                continue
            if len(packet) < 20 or (packet[0] >> 4) != 4:
                continue
            peer = self._select_outbound_peer(packet)
            if peer is None:
                self.metrics.inc("packet_drops")
                continue
            payload = self._build_multihop_payload(packet) if self.config.role == "client" else encode_route_layer(None, packet)
            if self.config.role == "client" and len(self.config.route_chain) < 2:
                payload = encode_route_layer(None, packet)
            ctx = PacketContext(peer_id=peer.peer_id, direction="outbound", control=False)
            payload = self.plugins.apply_send(payload, ctx)
            if payload is None:
                continue
            self._send_payload(peer, payload, MSG_DATA)

    def _keepalive_loop(self) -> None:
        while self.running.is_set():
            time.sleep(1.0)
            for peer in list(self.peers_by_id.values()):
                stale_after = max(30.0, peer.keepalive_seconds * 4)
                if time.time() - peer.last_seen > stale_after:
                    self.metrics.peer_inactive(peer.peer_id)
                if time.time() - peer.last_send >= peer.keepalive_seconds:
                    self._send_control(peer, encode_keepalive(time.monotonic_ns()))
                if peer.needs_rekey(self.config.session_max_seconds, self.config.session_max_bytes):
                    self.metrics.inc("rekey_count")
                    self.metrics.peer_rekey(peer.peer_id)
                    if peer.config and peer.config.host and peer.config.port:
                        self._initiate_handshake(peer.config)

    def _reconnect_loop(self) -> None:
        delay = 1.0
        while self.running.is_set():
            time.sleep(delay)
            active = any(
                time.time() - peer.last_seen < (peer.keepalive_seconds * 3)
                for peer in self.peers_by_id.values()
            )
            if active:
                delay = 1.0
                continue
            with self._connect_lock:
                self._connect_all()
            delay = min(delay * 2, self.config.reconnect_max_delay)

    def _stats_loop(self) -> None:
        while self.running.is_set():
            time.sleep(5.0)
            snap = self.metrics.snapshot()
            self.log.info(
                "peers=%s handshakes=%s rekeys=%s auth_failures=%s drops=%s",
                snap["active_peers"],
                snap["counters"].get("handshake_count", 0),
                snap["counters"].get("rekey_count", 0),
                snap["counters"].get("auth_failures", 0),
                snap["counters"].get("packet_drops", 0),
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="tinyvpn node runner")
    parser.add_argument("--config", required=True, help="Path to a tinyvpn JSON config file")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config)
    logging.basicConfig(
        level=getattr(logging, cfg.debug.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    node = VpnNode(cfg)
    try:
        node.start()
    finally:
        node.stop()
