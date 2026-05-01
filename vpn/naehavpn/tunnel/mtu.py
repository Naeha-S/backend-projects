"""MTU planning for the TUN and UDP tunnel layers."""

from __future__ import annotations

from dataclasses import dataclass

from ..crypto.session import SecureChannel
from ..data_plane.protocol import HEADER_LEN

ETHERNET_PATH_MTU = 1500
IPV4_UDP_OVERHEAD = 28
ROUTE_LAYER_RESERVE = 24
TUN_MTU_FLOOR = 1200
TUN_MTU_CEIL = 1400


@dataclass(slots=True)
class MtuProfile:
    tun_mtu: int
    payload_mtu: int
    wire_mtu: int
    fragment_payload_mtu: int
    overhead_bytes: int


def compute_mtu_profile(requested_tun_mtu: int) -> MtuProfile:
    wire_budget = min(TUN_MTU_CEIL + 100, ETHERNET_PATH_MTU - IPV4_UDP_OVERHEAD)
    tun_mtu = max(TUN_MTU_FLOOR, min(int(requested_tun_mtu), TUN_MTU_CEIL))
    overhead = HEADER_LEN + SecureChannel.TAG_BYTES + ROUTE_LAYER_RESERVE
    safe_tun_mtu = min(tun_mtu, wire_budget - overhead)
    safe_tun_mtu = max(TUN_MTU_FLOOR, safe_tun_mtu)
    fragment_payload_mtu = max(256, safe_tun_mtu - ROUTE_LAYER_RESERVE)
    wire_mtu = safe_tun_mtu + HEADER_LEN + SecureChannel.TAG_BYTES
    return MtuProfile(
        tun_mtu=safe_tun_mtu,
        payload_mtu=safe_tun_mtu,
        wire_mtu=wire_mtu,
        fragment_payload_mtu=fragment_payload_mtu,
        overhead_bytes=HEADER_LEN + SecureChannel.TAG_BYTES,
    )
