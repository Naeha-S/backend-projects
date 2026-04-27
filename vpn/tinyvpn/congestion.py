"""Adaptive pacing for UDP sends."""

from __future__ import annotations

import threading
import time


class AdaptivePacer:
    def __init__(self, initial_rate_bps: float = 8_000_000, burst_bytes: int = 128 * 1024):
        self._rate_bps = initial_rate_bps
        self._burst_bytes = burst_bytes
        self._tokens = burst_bytes
        self._last_refill = time.perf_counter()
        self._srtt_ms = 0.0
        self._lock = threading.Lock()

    def wait_for_send(self, size_bytes: int) -> None:
        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens >= size_bytes:
                    self._tokens -= size_bytes
                    return
                missing = size_bytes - self._tokens
                sleep_for = missing * 8 / max(self._rate_bps, 1.0)
            time.sleep(min(0.02, max(0.001, sleep_for)))

    def record_rtt(self, rtt_ms: float) -> None:
        with self._lock:
            if self._srtt_ms == 0:
                self._srtt_ms = rtt_ms
            else:
                self._srtt_ms = (0.875 * self._srtt_ms) + (0.125 * rtt_ms)
            if rtt_ms <= self._srtt_ms * 1.15:
                self._rate_bps = min(self._rate_bps * 1.05, 100_000_000)
            else:
                self._rate_bps = max(self._rate_bps * 0.90, 256_000)

    def record_loss(self) -> None:
        with self._lock:
            self._rate_bps = max(self._rate_bps * 0.70, 128_000)

    @property
    def rate_bps(self) -> float:
        return self._rate_bps

    @property
    def srtt_ms(self) -> float:
        return self._srtt_ms

    def _refill_locked(self) -> None:
        now = time.perf_counter()
        elapsed = now - self._last_refill
        self._last_refill = now
        self._tokens = min(self._burst_bytes, self._tokens + (elapsed * self._rate_bps / 8))
