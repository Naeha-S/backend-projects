"""Thread-safe metrics registry for logs and dashboard."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class MetricsRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._counters = defaultdict(int)
        self._peers = defaultdict(
            lambda: {
                "rx_bytes": 0,
                "tx_bytes": 0,
                "payload_rx_bytes": 0,
                "payload_tx_bytes": 0,
                "rx_packets": 0,
                "tx_packets": 0,
                "payload_rx_packets": 0,
                "payload_tx_packets": 0,
                "packet_loss": 0,
                "rtt_ms": 0.0,
                "rtt_avg_ms": 0.0,
                "rekeys": 0,
                "errors": 0,
                "last_seen": 0.0,
                "pacing_bps": 0.0,
                "last_handshake_ts": 0.0,
                "last_rekey_ts": 0.0,
                "last_error": "",
                "connection_mode": "unknown",
                "rx_rate_bps": 0.0,
                "tx_rate_bps": 0.0,
                "payload_rx_rate_bps": 0.0,
                "payload_tx_rate_bps": 0.0,
                "active": False,
            }
        )
        self._errors = deque(maxlen=64)
        self._rx_events = deque()
        self._tx_events = deque()
        self._payload_rx_events = deque()
        self._payload_tx_events = deque()
        self._peer_rx_events = defaultdict(deque)
        self._peer_tx_events = defaultdict(deque)
        self._peer_payload_rx_events = defaultdict(deque)
        self._peer_payload_tx_events = defaultdict(deque)
        self._peer_rtt_samples = defaultdict(lambda: deque(maxlen=32))
        self._started = time.time()
        self._connection_mode = "unknown"

    def set_connection_mode(self, mode: str) -> None:
        with self._lock:
            self._connection_mode = mode

    def register_peer(self, peer_id: str, connection_mode: str) -> None:
        with self._lock:
            peer = self._peers[peer_id]
            peer["connection_mode"] = connection_mode
            peer["last_handshake_ts"] = time.time()
            peer["last_seen"] = time.time()
            peer["active"] = True

    def inc(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] += amount

    def peer_rx(self, peer_id: str, size: int, *, payload: bool = False) -> None:
        with self._lock:
            now = time.time()
            peer = self._peers[peer_id]
            peer["rx_bytes"] += size
            peer["rx_packets"] += 1
            peer["last_seen"] = now
            peer["active"] = True
            self._rx_events.append((now, size))
            self._peer_rx_events[peer_id].append((now, size))
            if payload:
                peer["payload_rx_bytes"] += size
                peer["payload_rx_packets"] += 1
                self._payload_rx_events.append((now, size))
                self._peer_payload_rx_events[peer_id].append((now, size))

    def peer_tx(self, peer_id: str, size: int, *, payload: bool = False) -> None:
        with self._lock:
            now = time.time()
            peer = self._peers[peer_id]
            peer["tx_bytes"] += size
            peer["tx_packets"] += 1
            peer["last_seen"] = now
            peer["active"] = True
            self._tx_events.append((now, size))
            self._peer_tx_events[peer_id].append((now, size))
            if payload:
                peer["payload_tx_bytes"] += size
                peer["payload_tx_packets"] += 1
                self._payload_tx_events.append((now, size))
                self._peer_payload_tx_events[peer_id].append((now, size))

    def peer_loss(self, peer_id: str) -> None:
        with self._lock:
            self._peers[peer_id]["packet_loss"] += 1

    def peer_handshake(self, peer_id: str) -> None:
        with self._lock:
            self._peers[peer_id]["last_handshake_ts"] = time.time()
            self._peers[peer_id]["active"] = True

    def peer_rtt(self, peer_id: str, rtt_ms: float, pacing_bps: float) -> None:
        with self._lock:
            peer = self._peers[peer_id]
            samples = self._peer_rtt_samples[peer_id]
            samples.append(rtt_ms)
            peer["rtt_ms"] = round(rtt_ms, 3)
            peer["rtt_avg_ms"] = round(sum(samples) / len(samples), 3)
            peer["pacing_bps"] = round(pacing_bps, 2)

    def peer_rekey(self, peer_id: str) -> None:
        with self._lock:
            self._peers[peer_id]["rekeys"] += 1
            self._peers[peer_id]["last_rekey_ts"] = time.time()
            self._peers[peer_id]["active"] = True

    def peer_inactive(self, peer_id: str) -> None:
        with self._lock:
            if peer_id in self._peers:
                self._peers[peer_id]["active"] = False

    def peer_error(self, peer_id: str, message: str) -> None:
        with self._lock:
            self._peers[peer_id]["errors"] += 1
            self._peers[peer_id]["last_error"] = message
            self._errors.append({"ts": time.time(), "peer_id": peer_id, "message": message})

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            now = time.time()
            self._trim_events_locked(now)
            global_rx_bps = self._rate_from_events(self._rx_events)
            global_tx_bps = self._rate_from_events(self._tx_events)
            global_payload_rx_bps = self._rate_from_events(self._payload_rx_events)
            global_payload_tx_bps = self._rate_from_events(self._payload_tx_events)
            fragments_done = self._counters.get("fragment_reassembled", 0)
            fragments_total = fragments_done + self._counters.get("fragment_rx_pending", 0) + self._counters.get("fragment_tx", 0)
            fragmentation_ratio = (self._counters.get("fragment_tx", 0) / max(self._counters.get("packets_out", 0), 1))
            last_handshake_ts = 0.0
            last_rekey_ts = 0.0
            peer_snapshot = {}
            active_count = 0
            avg_rtt_ms = 0.0
            rtt_count = 0

            for peer_id, values in self._peers.items():
                item = dict(values)
                item["rx_rate_bps"] = round(self._rate_from_events(self._peer_rx_events[peer_id]), 2)
                item["tx_rate_bps"] = round(self._rate_from_events(self._peer_tx_events[peer_id]), 2)
                item["payload_rx_rate_bps"] = round(self._rate_from_events(self._peer_payload_rx_events[peer_id]), 2)
                item["payload_tx_rate_bps"] = round(self._rate_from_events(self._peer_payload_tx_events[peer_id]), 2)
                last_handshake_ts = max(last_handshake_ts, item["last_handshake_ts"])
                last_rekey_ts = max(last_rekey_ts, item["last_rekey_ts"])
                if item["active"]:
                    active_count += 1
                if item["rtt_avg_ms"] > 0:
                    avg_rtt_ms += item["rtt_avg_ms"]
                    rtt_count += 1
                peer_snapshot[peer_id] = item

            if rtt_count > 0:
                avg_rtt_ms = avg_rtt_ms / rtt_count

            return {
                "product": "NaehaVPN",
                "uptime_seconds": round(time.time() - self._started, 3),
                "active_peers": active_count,
                "connection_mode": self._connection_mode,
                "average_rtt_ms": round(avg_rtt_ms, 3) if avg_rtt_ms > 0 else 0,
                "rx_rate_bps": round(global_rx_bps, 2),
                "tx_rate_bps": round(global_tx_bps, 2),
                "payload_rx_rate_bps": round(global_payload_rx_bps, 2),
                "payload_tx_rate_bps": round(global_payload_tx_bps, 2),
                "fragmentation_ratio": round(fragmentation_ratio, 4),
                "fragment_activity_ratio": round((fragments_done / fragments_total), 4) if fragments_total else 0.0,
                "last_handshake_ts": last_handshake_ts,
                "last_rekey_ts": last_rekey_ts,
                "counters": dict(self._counters),
                "peers": peer_snapshot,
                "errors": list(self._errors),
            }

    def _trim_events_locked(self, now: float, window_seconds: float = 10.0) -> None:
        threshold = now - window_seconds
        for bucket in (
            self._rx_events,
            self._tx_events,
            self._payload_rx_events,
            self._payload_tx_events,
        ):
            while bucket and bucket[0][0] < threshold:
                bucket.popleft()
        for store in (
            self._peer_rx_events,
            self._peer_tx_events,
            self._peer_payload_rx_events,
            self._peer_payload_tx_events,
        ):
            for peer_id in list(store.keys()):
                bucket = store[peer_id]
                while bucket and bucket[0][0] < threshold:
                    bucket.popleft()

    def _rate_from_events(self, events: deque[tuple[float, int]], window_seconds: float = 10.0) -> float:
        if not events:
            return 0.0
        total = sum(size for _, size in events)
        return total / window_seconds
