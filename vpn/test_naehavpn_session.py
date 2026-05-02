#!/usr/bin/env python3
"""Regression tests for NaehaVPN session replay handling."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from naehavpn.crypto.session import ReplayWindow, SecureChannel


class TestReplayWindow(unittest.TestCase):
    def test_check_then_update_rejects_duplicate(self) -> None:
        window = ReplayWindow()
        self.assertTrue(window.check(5))
        window.update(5)
        self.assertFalse(window.check(5))

    def test_check_then_update_rejects_too_old(self) -> None:
        window = ReplayWindow()
        self.assertTrue(window.check(5000))
        window.update(5000)
        self.assertFalse(window.check(0))


class TestSecureChannel(unittest.TestCase):
    def test_failed_decrypt_does_not_advance_replay_window(self) -> None:
        key = os.urandom(32)
        sender = SecureChannel(key)
        receiver = SecureChannel(key)
        replay = ReplayWindow()
        aad = b"header"

        seq0 = sender.next_sequence()
        good0 = sender.encrypt(seq0, b"payload-0", aad)

        seq10 = 10
        bad10 = sender.encrypt(seq10, b"payload-10", aad[:-1] + b"x")
        self.assertIsNone(receiver.decrypt(seq10, bad10, aad, replay))

        self.assertEqual(receiver.decrypt(seq0, good0, aad, replay), b"payload-0")

    def test_successful_decrypt_updates_replay_window(self) -> None:
        key = os.urandom(32)
        sender = SecureChannel(key)
        receiver = SecureChannel(key)
        replay = ReplayWindow()
        aad = b"header"

        seq = sender.next_sequence()
        ciphertext = sender.encrypt(seq, b"payload", aad)
        self.assertEqual(receiver.decrypt(seq, ciphertext, aad, replay), b"payload")
        self.assertIsNone(receiver.decrypt(seq, ciphertext, aad, replay))


if __name__ == "__main__":
    unittest.main(verbosity=2)
