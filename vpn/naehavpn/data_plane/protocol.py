"""Wire protocol constants and packet encoding."""

from __future__ import annotations

import struct
from dataclasses import dataclass

VERSION = 0x31
MSG_DATA = 0x03
MSG_CONTROL = 0x04

CONTROL_KEEPALIVE = 0x01
CONTROL_KEEPALIVE_ACK = 0x02

HEADER_STRUCT = struct.Struct("!BBBBIQIHH")
HEADER_LEN = HEADER_STRUCT.size


@dataclass(slots=True)
class PacketHeader:
    version: int
    msg_type: int
    flags: int
    hop_count: int
    tunnel_id: int
    sequence: int
    fragment_id: int
    fragment_index: int
    fragment_count: int

    def pack(self) -> bytes:
        return HEADER_STRUCT.pack(
            self.version,
            self.msg_type,
            self.flags,
            self.hop_count,
            self.tunnel_id,
            self.sequence,
            self.fragment_id,
            self.fragment_index,
            self.fragment_count,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "PacketHeader":
        return cls(*HEADER_STRUCT.unpack(data[:HEADER_LEN]))


def build_header(
    *,
    msg_type: int,
    tunnel_id: int,
    sequence: int,
    fragment_id: int = 0,
    fragment_index: int = 0,
    fragment_count: int = 1,
    hop_count: int = 0,
    flags: int = 0,
) -> PacketHeader:
    return PacketHeader(
        version=VERSION,
        msg_type=msg_type,
        flags=flags,
        hop_count=hop_count,
        tunnel_id=tunnel_id,
        sequence=sequence,
        fragment_id=fragment_id,
        fragment_index=fragment_index,
        fragment_count=fragment_count,
    )


def encode_route_layer(next_peer_id: str | None, inner: bytes) -> bytes:
    if not next_peer_id:
        return b"\x00" + inner
    raw = next_peer_id.encode("ascii")
    if len(raw) > 255:
        raise ValueError("peer id is too long for route layer")
    return bytes([len(raw)]) + raw + inner


def decode_route_layer(payload: bytes) -> tuple[str | None, bytes]:
    if not payload:
        return None, b""
    size = payload[0]
    if size == 0:
        return None, payload[1:]
    if len(payload) < 1 + size:
        raise ValueError("malformed route layer")
    return payload[1 : 1 + size].decode("ascii"), payload[1 + size :]


def encode_keepalive(stamp_ns: int) -> bytes:
    return bytes([CONTROL_KEEPALIVE]) + struct.pack("!Q", stamp_ns)


def encode_keepalive_ack(stamp_ns: int) -> bytes:
    return bytes([CONTROL_KEEPALIVE_ACK]) + struct.pack("!Q", stamp_ns)


def decode_control(payload: bytes) -> tuple[int, int]:
    if len(payload) != 9:
        raise ValueError("malformed control payload")
    return payload[0], struct.unpack("!Q", payload[1:])[0]
