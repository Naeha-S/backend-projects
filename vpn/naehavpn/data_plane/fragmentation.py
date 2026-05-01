"""Payload fragmentation and reassembly with strict size bounds."""

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
    def __init__(self, max_payload_size: int, max_fragments: int = 8):
        self.max_payload_size = max_payload_size
        self.max_fragments = max_fragments

    def needs_fragmentation(self, payload: bytes) -> bool:
        return len(payload) > self.max_payload_size

    def fragment(self, payload: bytes) -> list[Fragment]:
        if len(payload) <= self.max_payload_size:
            return [Fragment(0, 0, 1, payload)]
        chunks = [
            payload[offset : offset + self.max_payload_size]
            for offset in range(0, len(payload), self.max_payload_size)
        ]
        if len(chunks) > self.max_fragments:
            raise ValueError("payload exceeds maximum fragment budget")
        fragment_id = int.from_bytes(os.urandom(4), "big")
        return [
            Fragment(fragment_id=fragment_id, index=index, count=len(chunks), payload=chunk)
            for index, chunk in enumerate(chunks)
        ]


@dataclass(slots=True)
class _Assembly:
    created_at: float
    count: int
    parts: dict[int, bytes] = field(default_factory=dict)
    total_bytes: int = 0


class Reassembler:
    def __init__(self, timeout_seconds: float = 15.0, max_fragments: int = 8, max_packet_size: int = 64 * 1024):
        self.timeout_seconds = timeout_seconds
        self.max_fragments = max_fragments
        self.max_packet_size = max_packet_size
        self._lock = threading.Lock()
        self._pending: dict[tuple[int, int], _Assembly] = {}

    def add(self, tunnel_id: int, fragment_id: int, index: int, count: int, payload: bytes) -> bytes | None:
        if count == 1:
            return payload
        if count < 1 or count > self.max_fragments or index >= count:
            return None
        key = (tunnel_id, fragment_id)
        with self._lock:
            self._evict_locked()
            state = self._pending.setdefault(key, _Assembly(time.time(), count))
            if index not in state.parts:
                state.total_bytes += len(payload)
            state.parts[index] = payload
            if state.total_bytes > self.max_packet_size:
                del self._pending[key]
                return None
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
