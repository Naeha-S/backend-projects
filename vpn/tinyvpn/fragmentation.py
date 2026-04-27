"""Fragmentation and reassembly for payloads larger than the effective MTU."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field


@dataclass(slots=True)
class Fragment:
    fragment_id: int
    index: int
    count: int
    payload: bytes


class Fragmenter:
    def __init__(self, max_payload_size: int):
        self.max_payload_size = max_payload_size

    def fragment(self, payload: bytes) -> list[Fragment]:
        if len(payload) <= self.max_payload_size:
            return [Fragment(0, 0, 1, payload)]
        fragment_id = int.from_bytes(os.urandom(4), "big")
        chunks = [
            payload[offset : offset + self.max_payload_size]
            for offset in range(0, len(payload), self.max_payload_size)
        ]
        return [
            Fragment(fragment_id=fragment_id, index=index, count=len(chunks), payload=chunk)
            for index, chunk in enumerate(chunks)
        ]


@dataclass
class _Assembly:
    created_at: float
    count: int
    parts: dict[int, bytes] = field(default_factory=dict)


class Reassembler:
    def __init__(self, timeout_seconds: float = 15.0):
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._pending: dict[tuple[int, int], _Assembly] = {}

    def add(self, tunnel_id: int, fragment_id: int, index: int, count: int, payload: bytes) -> bytes | None:
        if count == 1:
            return payload
        key = (tunnel_id, fragment_id)
        with self._lock:
            self._evict_locked()
            state = self._pending.setdefault(key, _Assembly(time.time(), count))
            state.parts[index] = payload
            if len(state.parts) != state.count:
                return None
            result = b"".join(state.parts[i] for i in range(state.count))
            del self._pending[key]
            return result

    def _evict_locked(self) -> None:
        now = time.time()
        stale = [
            key
            for key, value in self._pending.items()
            if now - value.created_at > self.timeout_seconds
        ]
        for key in stale:
            del self._pending[key]
