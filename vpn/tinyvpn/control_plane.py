"""Control plane: handshakes, keepalives, reconnection with resilience."""

from __future__ import annotations

import logging
import socket
import time
import threading
from dataclasses import dataclass, field
from collections import deque
from typing import Callable

log = logging.getLogger("naeha.control_plane")


@dataclass
class RttSample:
    """Single RTT measurement with timestamp."""
    rtt_ms: float
    timestamp: float
    sequence: int


class RttTracker:
    """Tracks round-trip time with rolling averages and jitter."""
    
    def __init__(self, window_size: int = 32):
        self.window_size = window_size
        self.samples: deque[RttSample] = deque(maxlen=window_size)
        self.lock = threading.Lock()
        self.sequence = 0
    
    def record(self, rtt_ms: float) -> None:
        """Record a new RTT sample."""
        with self.lock:
            self.sequence += 1
            sample = RttSample(
                rtt_ms=rtt_ms,
                timestamp=time.time(),
                sequence=self.sequence
            )
            self.samples.append(sample)
            log.debug(f"RTT sample: {rtt_ms:.2f}ms (seq={self.sequence})")
    
    def get_average(self) -> float:
        """Get average RTT from samples."""
        with self.lock:
            if not self.samples:
                return 0.0
            return sum(s.rtt_ms for s in self.samples) / len(self.samples)
    
    def get_min_max(self) -> tuple[float, float]:
        """Get min and max RTT."""
        with self.lock:
            if not self.samples:
                return 0.0, 0.0
            rtts = [s.rtt_ms for s in self.samples]
            return min(rtts), max(rtts)
    
    def get_jitter(self) -> float:
        """Calculate jitter (standard deviation of RTT)."""
        with self.lock:
            if len(self.samples) < 2:
                return 0.0
            avg = self.get_average()
            variance = sum((s.rtt_ms - avg) ** 2 for s in self.samples) / len(self.samples)
            return variance ** 0.5
    
    def get_latest(self) -> float:
        """Get most recent RTT."""
        with self.lock:
            if not self.samples:
                return 0.0
            return self.samples[-1].rtt_ms
    
    def snapshot(self) -> dict:
        """Return RTT snapshot."""
        with self.lock:
            if not self.samples:
                return {
                    "avg_ms": 0.0,
                    "min_ms": 0.0,
                    "max_ms": 0.0,
                    "jitter_ms": 0.0,
                    "latest_ms": 0.0,
                    "samples": 0,
                }
            min_rtt, max_rtt = self.get_min_max()
            avg_rtt = self.get_average()
            return {
                "avg_ms": round(avg_rtt, 3),
                "min_ms": round(min_rtt, 3),
                "max_ms": round(max_rtt, 3),
                "jitter_ms": round(self.get_jitter(), 3),
                "latest_ms": round(self.samples[-1].rtt_ms, 3),
                "samples": len(self.samples),
            }


class ReconnectStrategy:
    """Exponential backoff with jitter for reconnection."""
    
    def __init__(self, initial_delay: float = 1.0, max_delay: float = 30.0, jitter_factor: float = 0.1):
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.jitter_factor = jitter_factor
        self.current_delay = initial_delay
        self.attempt = 0
        self.lock = threading.Lock()
    
    def next_delay(self) -> float:
        """Get next reconnection delay with exponential backoff + jitter."""
        import random
        with self.lock:
            self.attempt += 1
            # Exponential backoff: 1s, 2s, 4s, 8s, ... capped at max_delay
            delay = min(self.initial_delay * (2 ** (self.attempt - 1)), self.max_delay)
            # Add jitter: ±jitter_factor%
            jitter = delay * random.uniform(-self.jitter_factor, self.jitter_factor)
            final_delay = delay + jitter
            log.info(f"Reconnect attempt {self.attempt}: waiting {final_delay:.2f}s")
            return max(final_delay, 0.1)  # Never less than 100ms
    
    def reset(self) -> None:
        """Reset backoff on successful connection."""
        with self.lock:
            self.attempt = 0
            self.current_delay = self.initial_delay
            log.debug("Reconnect strategy reset")
    
    def get_attempt_count(self) -> int:
        """Get current attempt number."""
        with self.lock:
            return self.attempt


class KeepaliveManager:
    """Manages periodic keepalive packets with timeout detection."""
    
    def __init__(self, interval_seconds: float = 15.0, timeout_seconds: float = 60.0):
        self.interval_seconds = interval_seconds
        self.timeout_seconds = timeout_seconds
        self.last_sent = time.time()
        self.last_received = time.time()
        self.lock = threading.Lock()
    
    def record_send(self) -> None:
        """Record that a keepalive was sent."""
        with self.lock:
            self.last_sent = time.time()
    
    def record_receive(self) -> None:
        """Record that a keepalive was received."""
        with self.lock:
            self.last_received = time.time()
    
    def should_send(self) -> bool:
        """Check if it's time to send a keepalive."""
        with self.lock:
            return (time.time() - self.last_sent) >= self.interval_seconds
    
    def is_stale(self) -> bool:
        """Check if peer is stale (no keepalives received)."""
        with self.lock:
            return (time.time() - self.last_received) > self.timeout_seconds
    
    def time_since_last_send(self) -> float:
        """Get time since last keepalive sent."""
        with self.lock:
            return time.time() - self.last_sent
    
    def time_since_last_receive(self) -> float:
        """Get time since last keepalive received."""
        with self.lock:
            return time.time() - self.last_received
