"""Optional packet decoding for debug mode."""

from __future__ import annotations

import struct


def decode_packet(packet: bytes) -> dict[str, object]:
    if len(packet) < 20:
        return {"layer3": "unknown", "length": len(packet)}
    version = packet[0] >> 4
    if version != 4:
        return {"layer3": f"ipv{version}", "length": len(packet)}
    ihl = (packet[0] & 0x0F) * 4
    info: dict[str, object] = {
        "layer3": "ipv4",
        "src": ".".join(str(x) for x in packet[12:16]),
        "dst": ".".join(str(x) for x in packet[16:20]),
        "ttl": packet[8],
        "protocol": packet[9],
        "total_length": struct.unpack("!H", packet[2:4])[0],
    }
    if packet[9] == 6 and len(packet) >= ihl + 20:
        src_port, dst_port = struct.unpack("!HH", packet[ihl : ihl + 4])
        info["layer4"] = {"type": "tcp", "src_port": src_port, "dst_port": dst_port}
    elif packet[9] == 17 and len(packet) >= ihl + 8:
        src_port, dst_port, length = struct.unpack("!HHH", packet[ihl : ihl + 6])
        info["layer4"] = {"type": "udp", "src_port": src_port, "dst_port": dst_port, "length": length}
    return info
