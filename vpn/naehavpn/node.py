"""Runtime node for NaehaVPN."""

from __future__ import annotations

import argparse
import logging
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass

from .config import NodeConfig, PeerConfig, load_config
from .crypto.session import (
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
from .data_plane.fragmentation import Fragmenter, Reassembler
from .data_plane.protocol import (
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
from .metrics import MetricsRegistry
from .networking import RouteManager, UdpTransport
from .tunnel import MtuProfile, TunDevice, compute_mtu_profile, create_tun
from tinyvpn.congestion import AdaptivePacer
from tinyvpn.dpi import decode_packet
from tinyvpn.plugins import LoggingPlugin, PacketContext, PacketFilterPlugin, PluginManager, TrafficShapingPlugin


@dataclass(slots=True)
class PendingHandshake:
    peer_name: str
    address: tuple[str, int]
    state: object
    created_at: float


class PeerState:
    def __init__(self, config: PeerConfig | None, material: SessionMaterial, address: tuple[str, int], mtu: MtuProfile, max_fragments: int):
        self.config = config
        self.peer_id = fingerprint(material.peer_static)
        self.material = material
        self.address = address
        self.virtual_ip = material.peer_virtual_ip
        self.keepalive_seconds = material.keepalive_seconds
        self.send_channel = SecureChannel(material.send_key)
        self.recv_channel = SecureChannel(material.recv_key)
        self.replay = ReplayWindow()
        self.fragmenter = Fragmenter(mtu.fragment_payload_mtu, max_fragments=max_fragments)
        self.reassembler = Reassembler(max_fragments=max_fragments, max_packet_size=mtu.payload_mtu * max_fragments)
        self.pacer = AdaptivePacer()
        self.last_seen = time.time()
        self.last_send = 0.0
        self.lock = threading.Lock()
        self.pending_pings: dict[int, float] = {}
        self.rtt_samples = deque(maxlen=32)

    def needs_rekey(self, max_seconds: int, max_bytes: int) -> bool:
        return self.send_channel.age_seconds >= max_seconds or self.send_channel.bytes_encrypted >= max_bytes


class NaehaVPNNode:
    def __init__(self, config: NodeConfig):
        self.config = config
        self.log = logging.getLogger("naehavpn")
        self.private_key = load_private_key(config.private_key_file)
        self.metrics = MetricsRegistry()
        self.metrics.set_connection_mode(self._describe_connection_mode())
        self.plugins = self._build_plugins()
        self.mtu = compute_mtu_profile(config.tun.mtu)
        self.route_manager = RouteManager(config.tun, config.peers)
        self.pending: dict[int, PendingHandshake] = {}
        self.peers_by_id: dict[str, PeerState] = {}
        self.peers_by_tunnel: dict[int, PeerState] = {}
        self.peers_by_ip: dict[str, PeerState] = {}
        self.peer_configs = {peer.name: peer for peer in config.peers}
        self.allowed_peer_ids = self._load_allowed_peer_ids()
        self.running = threading.Event()
        self.tun: TunDevice | None = None
        self.transport: UdpTransport | None = None
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
        self._open_tunnel()
        self.route_manager.apply()
        self.transport = UdpTransport(self.config.listen_host, self.config.listen_port)
        self.transport.start()

        if self.config.dashboard.enabled:
            self.dashboard = DashboardServer(
                self.metrics,
                self.config.dashboard.host,
                self.config.dashboard.port,
                self.config.dashboard.interval_seconds,
            )
            self.dashboard.start()

        self.log.info(
            "NaehaVPN %s connected node=%s tun=%s mtu=%s payload_mtu=%s wire_mtu=%s",
            self.config.role,
            self.config.node_name,
            self.config.tun.name,
            self.mtu.tun_mtu,
            self.mtu.fragment_payload_mtu,
            self.mtu.wire_mtu,
        )

        threading.Thread(target=self._recv_loop, daemon=True, name="udp-recv").start()
        threading.Thread(target=self._keepalive_loop, daemon=True, name="keepalive").start()
        threading.Thread(target=self._stats_loop, daemon=True, name="stats").start()

        if self.config.role == "client":
            self._connect_all()
            threading.Thread(target=self._reconnect_loop, daemon=True, name="reconnect").start()

        self._tun_loop()

    def stop(self) -> None:
        self.running.clear()
        if self.transport:
            self.transport.close()
        if self.tun:
            try:
                self.tun.close()
            except OSError:
                pass
        try:
            self.route_manager.cleanup()
        except Exception as exc:
            self.log.debug("route cleanup failed: %s", exc)

    def _open_tunnel(self) -> None:
        self.tun = create_tun(
            self.config.tun.name,
            self.config.tun.address,
            self.config.tun.netmask,
            self.mtu.tun_mtu,
        )

    def _reopen_tunnel(self) -> None:
        try:
            if self.tun:
                self.tun.close()
        except OSError:
            pass
        self._open_tunnel()

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
        assert self.transport is not None
        self.transport.enqueue(wire, (peer_cfg.host, peer_cfg.port))
        self.metrics.inc("handshake_count")

    def _install_peer(self, peer_cfg: PeerConfig | None, material: SessionMaterial, address: tuple[str, int]) -> PeerState:
        existing = self.peers_by_id.get(fingerprint(material.peer_static))
        if existing is not None:
            self.peers_by_tunnel.pop(existing.material.tunnel_id, None)
            if existing.virtual_ip:
                self.peers_by_ip.pop(existing.virtual_ip, None)
        peer = PeerState(peer_cfg, material, address, self.mtu, self.config.max_fragments_per_packet)
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
        assert self.transport is not None
        while self.running.is_set():
            try:
                packet, address = self.transport.recv()
            except socket.timeout:
                continue
            except OSError as exc:
                self.log.debug("Socket receive error: %s", exc)
                continue

            if not packet:
                continue
            self.metrics.inc("socket_packets_received")
            self.log.info("RECV: raw packet size = %d", len(packet))
            if packet[0] == HANDSHAKE_INIT:
                self._handle_handshake_init(packet, address)
                continue
            if packet[0] == HANDSHAKE_REPLY:
                self._handle_handshake_reply(packet, address)
                continue
            if len(packet) < HEADER_LEN:
                self.metrics.inc("dropped_packets")
                continue

            header = PacketHeader.unpack(packet)
            if header.version != VERSION:
                self.metrics.inc("dropped_packets")
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
        assert self.transport is not None
        self.transport.enqueue(reply, address)
        self.metrics.inc("handshake_count")
        self.log.info("NaehaVPN peer connected peer=%s addr=%s", peer.peer_id, address)
        self._send_keepalive(peer)

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

        rtt_ms = (time.time() - pending.created_at) * 1000
        peer_cfg = self.peer_configs[pending.peer_name]
        if peer_cfg.advertised_tun_ip:
            material.peer_virtual_ip = peer_cfg.advertised_tun_ip
        peer = self._install_peer(peer_cfg, material, address)
        self.metrics.peer_rtt(peer.peer_id, rtt_ms, peer.pacer.rate_bps)
        self.log.info("NaehaVPN handshake complete peer=%s addr=%s rtt_ms=%.1f", peer.peer_id, address, rtt_ms)
        self._send_keepalive(peer)

    def _handle_encrypted(self, peer: PeerState, header: PacketHeader, ciphertext: bytes, address: tuple[str, int]) -> None:
        aad = header.pack()
        plaintext = peer.recv_channel.decrypt(header.sequence, ciphertext, aad, peer.replay)
        if plaintext is None:
            self.log.error("DECRYPT FAIL: error (seq=%d)", header.sequence)
            self.metrics.inc("packets_decrypted_failed")
            self.metrics.inc("auth_failures")
            self.metrics.inc("encryption_errors")
            self.metrics.peer_loss(peer.peer_id)
            self.metrics.peer_error(peer.peer_id, f"decrypt-failed msg_type={header.msg_type} tunnel_id={header.tunnel_id}")
            peer.pacer.record_loss()
            return

        if peer.address != address:
            peer.address = address
        peer.last_seen = time.time()
        self.log.info("DECRYPT OK: size = %d", len(plaintext))
        self.metrics.inc("packets_decrypted_success")
        self.metrics.inc("packets_in")
        self.metrics.peer_rx(peer.peer_id, len(ciphertext))

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
        if header.fragment_count > 1:
            self.metrics.inc("fragment_reassembled")

        if header.msg_type == MSG_CONTROL:
            self._handle_control(peer, payload)
            return

        try:
            next_peer_id, inner = decode_route_layer(payload)
        except Exception:
            self.metrics.inc("dropped_packets")
            self.metrics.peer_error(peer.peer_id, "malformed route layer")
            return
        if next_peer_id:
            self._forward_to_peer(next_peer_id, inner)
            return
        if self.config.debug.dpi:
            self.log.debug("dpi %s", decode_packet(inner))
        if len(inner) > self.mtu.tun_mtu:
            self.metrics.inc("dropped_packets")
            self.metrics.peer_error(peer.peer_id, f"inbound packet larger than tun mtu ({len(inner)})")
            return
        assert self.tun is not None
        self.log.info("WRITE TUN: size = %d", len(inner))
        self.metrics.inc("packets_written_to_tun")
        try:
            self.tun.write(inner)
        except OSError as exc:
            self.metrics.inc("dropped_packets")
            self.metrics.peer_error(peer.peer_id, f"tun write failed: {exc}")
            self._reopen_tunnel()
            return
        self.metrics.peer_rx(peer.peer_id, len(inner), payload=True)

    def _handle_control(self, peer: PeerState, payload: bytes) -> None:
        try:
            ctrl_type, value = decode_control(payload)
        except ValueError:
            self.metrics.inc("dropped_packets")
            self.metrics.peer_error(peer.peer_id, "malformed control payload")
            return
        if ctrl_type == CONTROL_KEEPALIVE:
            self._send_control(peer, encode_keepalive_ack(value))
        elif ctrl_type == CONTROL_KEEPALIVE_ACK:
            sent_at = peer.pending_pings.pop(value, None)
            if sent_at is None:
                return
            rtt_ms = (time.monotonic_ns() - value) / 1_000_000
            peer.rtt_samples.append(rtt_ms)
            peer.pacer.record_rtt(rtt_ms)
            self.metrics.peer_rtt(peer.peer_id, rtt_ms, peer.pacer.rate_bps)

    def _forward_to_peer(self, peer_id: str, inner_wire: bytes) -> None:
        peer = self.peers_by_id.get(peer_id)
        if peer is None or self.transport is None:
            self.metrics.inc("dropped_packets")
            return
        peer.pacer.wait_for_send(len(inner_wire))
        if not self.transport.enqueue(inner_wire, peer.address):
            self.metrics.inc("dropped_packets")
            return
        self.metrics.inc("packets_out")
        self.metrics.peer_tx(peer.peer_id, len(inner_wire))

    def _build_wires(self, peer: PeerState, payload: bytes, msg_type: int) -> list[bytes]:
        wires = []
        try:
            fragments = peer.fragmenter.fragment(payload)
        except ValueError:
            self.metrics.inc("dropped_packets")
            self.metrics.peer_error(peer.peer_id, f"payload too large ({len(payload)} bytes)")
            return []
        if len(fragments) > 1:
            self.metrics.inc("fragment_tx")
        for fragment in fragments:
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
            wire = aad + ciphertext
            if len(wire) > self.mtu.wire_mtu:
                self.metrics.inc("dropped_packets")
                self.metrics.peer_error(peer.peer_id, f"wire packet exceeded budget ({len(wire)} > {self.mtu.wire_mtu})")
                return []
            wires.append(wire)
        return wires

    def _send_payload(self, peer: PeerState, payload: bytes, msg_type: int) -> None:
        assert self.transport is not None
        wires = self._build_wires(peer, payload, msg_type)
        payload_bytes = 0 if msg_type == MSG_CONTROL else len(payload)
        for wire in wires:
            peer.pacer.wait_for_send(len(wire))
            if not self.transport.enqueue(wire, peer.address):
                self.metrics.inc("dropped_packets")
                continue
            peer.last_send = time.time()
            self.metrics.inc("packets_out")
            self.metrics.peer_tx(peer.peer_id, len(wire))
        if payload_bytes:
            self.metrics.peer_tx(peer.peer_id, payload_bytes, payload=True)

    def _send_control(self, peer: PeerState, payload: bytes) -> None:
        self._send_payload(peer, payload, MSG_CONTROL)

    def _send_keepalive(self, peer: PeerState) -> None:
        stamp = time.monotonic_ns()
        peer.pending_pings[stamp] = time.time()
        self._send_control(peer, encode_keepalive(stamp))

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
            return encode_route_layer(None, packet)
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
        read_size = max(65535, self.mtu.tun_mtu + 256)
        while self.running.is_set():
            try:
                packet = self.tun.read(read_size)
            except OSError as exc:
                self.log.warning("tun read failed, reopening interface: %s", exc)
                self._reopen_tunnel()
                continue
            if not self.peers_by_id:
                continue
            if len(packet) < 20 or (packet[0] >> 4) != 4:
                continue
            if len(packet) > self.mtu.tun_mtu:
                self.metrics.inc("dropped_packets")
                continue
            peer = self._select_outbound_peer(packet)
            if peer is None:
                self.metrics.inc("dropped_packets")
                continue
            try:
                payload = self._build_multihop_payload(packet) if self.config.role == "client" else encode_route_layer(None, packet)
            except RuntimeError as exc:
                self.metrics.inc("dropped_packets")
                self.metrics.peer_error(peer.peer_id, str(exc))
                continue
            ctx = PacketContext(peer_id=peer.peer_id, direction="outbound", control=False)
            payload = self.plugins.apply_send(payload, ctx)
            if payload is None:
                continue
            self._send_payload(peer, payload, MSG_DATA)

    def _keepalive_loop(self) -> None:
        while self.running.is_set():
            time.sleep(1.0)
            now = time.time()
            for peer in list(self.peers_by_id.values()):
                stale_after = max(30.0, peer.keepalive_seconds * self.config.keepalive_timeout_multiplier)
                if now - peer.last_seen > stale_after:
                    self.metrics.peer_inactive(peer.peer_id)
                if now - peer.last_send >= peer.keepalive_seconds:
                    self._send_keepalive(peer)
                expired = [
                    stamp
                    for stamp, sent_at in list(peer.pending_pings.items())
                    if now - sent_at > stale_after
                ]
                for stamp in expired:
                    peer.pending_pings.pop(stamp, None)
                    self.metrics.peer_loss(peer.peer_id)
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
                "NaehaVPN stats peers=%s rx=%sKB/s tx=%sKB/s payload_rx=%sKB/s payload_tx=%sKB/s rtt=%.1fms rekeys=%s drops=%s frag=%.1f%%",
                snap["active_peers"],
                round(float(snap["rx_rate_bps"]) / 1024, 2),
                round(float(snap["tx_rate_bps"]) / 1024, 2),
                round(float(snap["payload_rx_rate_bps"]) / 1024, 2),
                round(float(snap["payload_tx_rate_bps"]) / 1024, 2),
                float(snap["average_rtt_ms"]),
                snap["counters"].get("rekey_count", 0),
                snap["counters"].get("dropped_packets", 0),
                float(snap["fragmentation_ratio"]) * 100,
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NaehaVPN node runner")
    parser.add_argument("--config", required=True, help="Path to a NaehaVPN JSON config file")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config)
    logging.basicConfig(
        level=getattr(logging, cfg.debug.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    node = NaehaVPNNode(cfg)
    try:
        node.start()
    finally:
        node.stop()
