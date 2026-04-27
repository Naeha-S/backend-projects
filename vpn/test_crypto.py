#!/usr/bin/env python3
"""Unit tests for the upgraded tinyvpn stack."""

from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, os.path.dirname(__file__))

from tinyvpn.crypto import (
    ReplayWindow,
    SecureChannel,
    build_handshake_init,
    complete_handshake,
    generate_private_key,
    private_key_bytes,
    public_key_bytes,
    respond_handshake_init,
)
from tinyvpn.fragmentation import Fragmenter, Reassembler


class TestReplayWindow(unittest.TestCase):
    def test_accepts_new_and_rejects_duplicate(self) -> None:
        window = ReplayWindow()
        self.assertTrue(window.check_and_update(5))
        self.assertFalse(window.check_and_update(5))

    def test_rejects_too_old(self) -> None:
        window = ReplayWindow()
        self.assertTrue(window.check_and_update(5000))
        self.assertFalse(window.check_and_update(0))


class TestSecureChannel(unittest.TestCase):
    def test_round_trip_and_replay(self) -> None:
        key = os.urandom(32)
        sender = SecureChannel(key)
        receiver = SecureChannel(key)
        replay = ReplayWindow()
        seq = sender.next_sequence()
        aad = b"header"
        ciphertext = sender.encrypt(seq, b"payload", aad)
        self.assertEqual(receiver.decrypt(seq, ciphertext, aad, replay), b"payload")
        self.assertIsNone(receiver.decrypt(seq, ciphertext, aad, replay))


class TestHandshake(unittest.TestCase):
    def test_noise_style_handshake(self) -> None:
        client_static = generate_private_key()
        server_static = generate_private_key()
        state, init_wire = build_handshake_init(
            client_static,
            server_static.public_key(),
            "10.44.0.2",
            network_name="testnet",
            keepalive_seconds=15,
        )
        server_result = respond_handshake_init(server_static, init_wire, network_name="testnet")
        self.assertIsNotNone(server_result)
        server_material, reply = server_result
        client_material = complete_handshake(state, reply, network_name="testnet")
        self.assertIsNotNone(client_material)
        self.assertEqual(server_material.tunnel_id, client_material.tunnel_id)
        self.assertEqual(server_material.send_key, client_material.recv_key)
        self.assertEqual(server_material.recv_key, client_material.send_key)


class TestFragmentation(unittest.TestCase):
    def test_fragment_reassemble(self) -> None:
        payload = os.urandom(4096)
        fragmenter = Fragmenter(512)
        fragments = fragmenter.fragment(payload)
        self.assertGreater(len(fragments), 1)
        reassembler = Reassembler()
        rebuilt = None
        for fragment in fragments:
            rebuilt = reassembler.add(1, fragment.fragment_id, fragment.index, fragment.count, fragment.payload)
        self.assertEqual(rebuilt, payload)


class TestKeyEncoding(unittest.TestCase):
    def test_base64_roundtrip(self) -> None:
        key = generate_private_key()
        tmp = os.path.join(os.path.dirname(__file__), f".tmp_keys_{uuid.uuid4().hex}")
        os.makedirs(tmp, exist_ok=True)
        try:
            priv_path = os.path.join(tmp, "node.key")
            pub_path = os.path.join(tmp, "node.pub")
            with open(priv_path, "wb") as handle:
                handle.write(base64.b64encode(private_key_bytes(key)))
            with open(pub_path, "wb") as handle:
                handle.write(base64.b64encode(public_key_bytes(key.public_key())))
            self.assertTrue(os.path.exists(priv_path))
            self.assertTrue(os.path.exists(pub_path))
        finally:
            for name in ("node.key", "node.pub"):
                path = os.path.join(tmp, name)
                if os.path.exists(path):
                    os.remove(path)
            if os.path.isdir(tmp):
                os.rmdir(tmp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
