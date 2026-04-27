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
                "rx_packets": 0,
                "tx_packets": 0,
                "packet_loss": 0,
                "rtt_ms": 0.0,
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
                "active": False,
            }
        )
        self._errors = deque(maxlen=64)
        self._rx_events = deque()
        self._tx_events = deque()
        self._peer_rx_events = defaultdict(deque)
        self._peer_tx_events = defaultdict(deque)
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

    def peer_rx(self, peer_id: str, size: int) -> None:
        with self._lock:
            peer = self._peers[peer_id]
            peer["rx_bytes"] += size
            peer["rx_packets"] += 1
            peer["last_seen"] = time.time()
            peer["active"] = True
            self._rx_events.append((time.time(), size))
            self._peer_rx_events[peer_id].append((time.time(), size))

    def peer_tx(self, peer_id: str, size: int) -> None:
        with self._lock:
            peer = self._peers[peer_id]
            peer["tx_bytes"] += size
            peer["tx_packets"] += 1
            peer["last_seen"] = time.time()
            peer["active"] = True
            self._tx_events.append((time.time(), size))
            self._peer_tx_events[peer_id].append((time.time(), size))

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
            peer["rtt_ms"] = round(rtt_ms, 3)
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
            fragments_done = self._counters.get("fragment_reassembled", 0)
            fragments_total = fragments_done + self._counters.get("fragment_rx_pending", 0)
            fragmentation_ratio = (fragments_done / fragments_total) if fragments_total else 0.0
            last_handshake_ts = 0.0
            last_rekey_ts = 0.0
            peer_snapshot = {}
            active_count = 0
            for peer_id, values in self._peers.items():
                item = dict(values)
                item["rx_rate_bps"] = round(self._rate_from_events(self._peer_rx_events[peer_id]), 2)
                item["tx_rate_bps"] = round(self._rate_from_events(self._peer_tx_events[peer_id]), 2)
                last_handshake_ts = max(last_handshake_ts, item["last_handshake_ts"])
                last_rekey_ts = max(last_rekey_ts, item["last_rekey_ts"])
                if item["active"]:
                    active_count += 1
                peer_snapshot[peer_id] = item
            return {
                "uptime_seconds": round(time.time() - self._started, 3),
                "active_peers": active_count,
                "connection_mode": self._connection_mode,
                "rx_rate_bps": round(global_rx_bps, 2),
                "tx_rate_bps": round(global_tx_bps, 2),
                "fragmentation_ratio": round(fragmentation_ratio, 4),
                "last_handshake_ts": last_handshake_ts,
                "last_rekey_ts": last_rekey_ts,
                "counters": dict(self._counters),
                "peers": peer_snapshot,
                "errors": list(self._errors),
            }

    def _trim_events_locked(self, now: float, window_seconds: float = 10.0) -> None:
        threshold = now - window_seconds
        for bucket in (self._rx_events, self._tx_events):
            while bucket and bucket[0][0] < threshold:
                bucket.popleft()
        for peer_id in list(self._peer_rx_events.keys()):
            bucket = self._peer_rx_events[peer_id]
            while bucket and bucket[0][0] < threshold:
                bucket.popleft()
        for peer_id in list(self._peer_tx_events.keys()):
            bucket = self._peer_tx_events[peer_id]
            while bucket and bucket[0][0] < threshold:
                bucket.popleft()

    def _rate_from_events(self, events: deque[tuple[float, int]], window_seconds: float = 10.0) -> float:
        if not events:
            return 0.0
        total = sum(size for _, size in events)
        return total / window_seconds
