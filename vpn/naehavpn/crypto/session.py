"""Cryptography, handshake helpers, and anti-replay state."""

from __future__ import annotations

import base64
import os
import struct
import threading
import time
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

HANDSHAKE_INIT = 0x01
HANDSHAKE_REPLY = 0x02


def hkdf(material: bytes, *, salt: bytes = b"", info: bytes = b"", length: int = 32) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=length,
        salt=salt or None,
        info=info,
    ).derive(material)


def public_key_bytes(key: X25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def private_key_bytes(key: X25519PrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )


def generate_private_key() -> X25519PrivateKey:
    return X25519PrivateKey.generate()


def load_private_key(path: str) -> X25519PrivateKey:
    raw = base64.b64decode(open(path, "rb").read().strip())
    return X25519PrivateKey.from_private_bytes(raw)


def load_public_key(path: str) -> X25519PublicKey:
    raw = base64.b64decode(open(path, "rb").read().strip())
    return X25519PublicKey.from_public_bytes(raw)


def fingerprint(public_key_raw: bytes) -> str:
    return hkdf(public_key_raw, info=b"naehavpn-fingerprint", length=8).hex()


class ReplayWindow:
    """Sliding bitmap used for anti-replay checks."""

    WINDOW_SIZE = 2048

    def __init__(self):
        self._highest = 0
        self._bitmap = 0
        self._lock = threading.Lock()

    def check(self, counter: int) -> bool:
        with self._lock:
            if counter > self._highest:
                return True
            diff = self._highest - counter
            if diff >= self.WINDOW_SIZE:
                return False
            bit = 1 << diff
            if self._bitmap & bit:
                return False
            return True

    def update(self, counter: int) -> None:
        with self._lock:
            if counter > self._highest:
                diff = counter - self._highest
                if diff >= self.WINDOW_SIZE:
                    self._bitmap = 1
                else:
                    self._bitmap = ((self._bitmap << diff) | 1) & ((1 << self.WINDOW_SIZE) - 1)
                self._highest = counter
            else:
                diff = self._highest - counter
                if diff < self.WINDOW_SIZE:
                    self._bitmap |= (1 << diff)

    def reset(self) -> None:
        with self._lock:
            self._highest = 0
            self._bitmap = 0


class SecureChannel:
    """AES-256-GCM channel using explicit sequence numbers."""

    TAG_BYTES = 16

    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError("AES-256-GCM requires a 32-byte key")
        self._aes = AESGCM(key)
        self._lock = threading.Lock()
        self._next_sequence = 0
        self._created = time.time()
        self._bytes = 0

    def next_sequence(self) -> int:
        with self._lock:
            seq = self._next_sequence
            self._next_sequence += 1
            return seq

    def encrypt(self, sequence: int, plaintext: bytes, aad: bytes) -> bytes:
        with self._lock:
            self._bytes += len(plaintext)
        nonce = b"\x00\x00\x00\x00" + struct.pack("!Q", sequence)
        return self._aes.encrypt(nonce, plaintext, aad)

    def decrypt(self, sequence: int, ciphertext: bytes, aad: bytes, replay: ReplayWindow) -> bytes | None:
        if not replay.check(sequence):
            return None
        nonce = b"\x00\x00\x00\x00" + struct.pack("!Q", sequence)
        try:
            plaintext = self._aes.decrypt(nonce, ciphertext, aad)
            replay.update(sequence)
            return plaintext
        except Exception:
            return None

    @property
    def age_seconds(self) -> float:
        return time.time() - self._created

    @property
    def bytes_encrypted(self) -> int:
        return self._bytes


@dataclass(slots=True)
class HandshakeInitState:
    session_id: int
    local_static: X25519PrivateKey
    remote_static: X25519PublicKey
    ephemeral_private: X25519PrivateKey
    advertised_ip: str


@dataclass(slots=True)
class SessionMaterial:
    tunnel_id: int
    peer_static: bytes
    peer_virtual_ip: str
    keepalive_seconds: int
    send_key: bytes
    recv_key: bytes


def build_handshake_init(
    local_static: X25519PrivateKey,
    remote_static: X25519PublicKey,
    advertised_ip: str,
    *,
    network_name: str,
    keepalive_seconds: int,
) -> tuple[HandshakeInitState, bytes]:
    eph = X25519PrivateKey.generate()
    eph_raw = public_key_bytes(eph.public_key())
    local_raw = public_key_bytes(local_static.public_key())
    session_id = int.from_bytes(os.urandom(4), "big")
    init_key = hkdf(
        eph.exchange(remote_static),
        salt=network_name.encode("utf-8"),
        info=b"naehavpn-noise-init",
    )
    ip_bytes = bytes(int(part) for part in advertised_ip.split("."))
    payload = local_raw + ip_bytes + struct.pack("!H", keepalive_seconds)
    nonce = struct.pack("!I", session_id) + b"\x00" * 8
    ciphertext = AESGCM(init_key).encrypt(nonce, payload, eph_raw)
    state = HandshakeInitState(session_id, local_static, remote_static, eph, advertised_ip)
    return state, bytes([HANDSHAKE_INIT]) + struct.pack("!I", session_id) + eph_raw + ciphertext


def respond_handshake_init(
    local_static: X25519PrivateKey,
    packet: bytes,
    *,
    network_name: str,
) -> tuple[SessionMaterial, bytes] | None:
    if len(packet) < 91:
        return None
    session_id = struct.unpack("!I", packet[1:5])[0]
    initiator_eph_raw = packet[5:37]
    initiator_eph = X25519PublicKey.from_public_bytes(initiator_eph_raw)
    init_key = hkdf(
        local_static.exchange(initiator_eph),
        salt=network_name.encode("utf-8"),
        info=b"naehavpn-noise-init",
    )
    nonce = struct.pack("!I", session_id) + b"\x00" * 8
    try:
        payload = AESGCM(init_key).decrypt(nonce, packet[37:], initiator_eph_raw)
    except Exception:
        return None
    if len(payload) != 38:
        return None
    peer_static_raw = payload[:32]
    peer_static = X25519PublicKey.from_public_bytes(peer_static_raw)
    virtual_ip = ".".join(str(part) for part in payload[32:36])
    keepalive_seconds = max(5, struct.unpack("!H", payload[36:38])[0])

    responder_eph = X25519PrivateKey.generate()
    responder_eph_raw = public_key_bytes(responder_eph.public_key())
    master = hkdf(
        local_static.exchange(initiator_eph)
        + responder_eph.exchange(initiator_eph)
        + local_static.exchange(peer_static)
        + responder_eph.exchange(peer_static),
        salt=struct.pack("!I", session_id),
        info=b"naehavpn-noise-master",
        length=64,
    )
    init_to_resp, resp_to_init = master[:32], master[32:64]
    tunnel_id = int.from_bytes(os.urandom(4), "big")
    reply_key = hkdf(master, info=b"naehavpn-noise-reply")
    reply_payload = struct.pack("!IH", tunnel_id, keepalive_seconds)
    reply_nonce = struct.pack("!I", session_id) + b"\x01" * 8
    reply_ct = AESGCM(reply_key).encrypt(reply_nonce, reply_payload, responder_eph_raw)
    material = SessionMaterial(
        tunnel_id=tunnel_id,
        peer_static=peer_static_raw,
        peer_virtual_ip=virtual_ip,
        keepalive_seconds=keepalive_seconds,
        send_key=resp_to_init,
        recv_key=init_to_resp,
    )
    reply = bytes([HANDSHAKE_REPLY]) + struct.pack("!I", session_id) + responder_eph_raw + reply_ct
    return material, reply


def complete_handshake(
    state: HandshakeInitState,
    packet: bytes,
    *,
    network_name: str,
) -> SessionMaterial | None:
    if len(packet) < 59 or packet[0] != HANDSHAKE_REPLY:
        return None
    session_id = struct.unpack("!I", packet[1:5])[0]
    if session_id != state.session_id:
        return None
    responder_eph_raw = packet[5:37]
    responder_eph = X25519PublicKey.from_public_bytes(responder_eph_raw)
    master = hkdf(
        state.ephemeral_private.exchange(state.remote_static)
        + state.ephemeral_private.exchange(responder_eph)
        + state.local_static.exchange(state.remote_static)
        + state.local_static.exchange(responder_eph),
        salt=struct.pack("!I", session_id),
        info=b"naehavpn-noise-master",
        length=64,
    )
    init_to_resp, resp_to_init = master[:32], master[32:64]
    reply_key = hkdf(master, info=b"naehavpn-noise-reply")
    try:
        reply_payload = AESGCM(reply_key).decrypt(
            struct.pack("!I", session_id) + b"\x01" * 8,
            packet[37:],
            responder_eph_raw,
        )
    except Exception:
        return None
    if len(reply_payload) != 6:
        return None
    tunnel_id, keepalive_seconds = struct.unpack("!IH", reply_payload)
    return SessionMaterial(
        tunnel_id=tunnel_id,
        peer_static=public_key_bytes(state.remote_static),
        peer_virtual_ip="0.0.0.0",
        keepalive_seconds=max(5, keepalive_seconds),
        send_key=init_to_resp,
        recv_key=resp_to_init,
    )
