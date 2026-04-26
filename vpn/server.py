#!/usr/bin/env python3
"""
server.py — tinyvpn server
──────────────────────────
Run on the machine that has a public IP (or a reachable port).
Must be run as root (TUN device + network config require it).

Usage:
    KEY=$(python3 -c "import os; print(os.urandom(32).hex())")
    sudo python3 server.py --key $KEY

    # or with a custom port / interface address:
    sudo python3 server.py --key $KEY --port 9999 --ip 10.8.0.1
"""

import os
import sys
import socket
import struct
import threading
import argparse
import time
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Make sure tun.py is importable from the same directory
sys.path.insert(0, os.path.dirname(__file__))
from tun import open_tun, configure_iface


# ── Packet counters (shared between threads) ──────────────────────────────────
_stats = {"rx_pkts": 0, "tx_pkts": 0, "auth_fail": 0, "rx_bytes": 0, "tx_bytes": 0}
_stats_lock = threading.Lock()


def _count(key: str, n: int = 1):
    with _stats_lock:
        _stats[key] += n


# ── Core loops ─────────────────────────────────────────────────────────────────

def tun_to_udp(tun_fd: int, sock: socket.socket, aes: AESGCM, peer: tuple):
    """
    Read loop: kernel → TUN fd → encrypt → UDP socket → peer.

    Each iteration:
      1. os.read() blocks until the kernel has a full IP packet for us.
      2. We generate a fresh 12-byte random nonce (NIST SP 800-38D §8.2.2).
      3. AESGCM.encrypt() seals: ciphertext + 16-byte GHASH authentication tag.
      4. We prepend the nonce and fire the datagram.

    Wire format:  [ nonce 12B ][ ciphertext + tag (len = plaintext + 16) ]
    """
    while True:
        try:
            packet = os.read(tun_fd, 65535)
        except OSError as e:
            print(f"[!] TUN read error: {e}")
            break

        nonce      = os.urandom(12)
        ciphertext = aes.encrypt(nonce, packet, None)   # None = no AAD
        wire       = nonce + ciphertext

        try:
            sock.sendto(wire, peer)
            _count("tx_pkts")
            _count("tx_bytes", len(wire))
        except OSError as e:
            print(f"[!] UDP send error: {e}")


def udp_to_tun(tun_fd: int, sock: socket.socket, aes: AESGCM, peer_ref: list):
    """
    Receive loop: UDP socket → authenticate+decrypt → TUN fd → kernel.

    peer_ref is a mutable list so we can update the peer address when we
    see the first (handshake) datagram from the client.

    If decryption fails (wrong key, truncated, bit-flipped) we drop the
    packet and log the failure — no partial plaintext ever reaches the TUN.
    """
    while True:
        try:
            data, addr = sock.recvfrom(65535 + 12 + 16)
        except OSError as e:
            print(f"[!] UDP recv error: {e}")
            break

        # Update peer address dynamically (handles NAT re-keying / reconnect)
        if peer_ref[0] is None or addr != peer_ref[0]:
            peer_ref[0] = addr
            print(f"[+] Peer connected: {addr[0]}:{addr[1]}")

        if len(data) < 12 + 1:          # nonce(12) + at least 1B ciphertext
            continue                     # too short to be a valid frame

        nonce      = data[:12]
        ciphertext = data[12:]

        try:
            packet = aes.decrypt(nonce, ciphertext, None)
        except Exception:
            _count("auth_fail")
            # Do NOT log frequently — could be a flood attack
            with _stats_lock:
                if _stats["auth_fail"] % 100 == 1:
                    print(f"[!] Auth failures so far: {_stats['auth_fail']}")
            continue

        try:
            os.write(tun_fd, packet)
            _count("rx_pkts")
            _count("rx_bytes", len(packet))
        except OSError as e:
            print(f"[!] TUN write error: {e}")


# ── Stats printer ─────────────────────────────────────────────────────────────

def stats_printer(interval: int = 5):
    """Print a one-liner every `interval` seconds."""
    while True:
        time.sleep(interval)
        with _stats_lock:
            s = dict(_stats)
        print(
            f"[~] rx={s['rx_pkts']} pkts ({s['rx_bytes']//1024} KB)  "
            f"tx={s['tx_pkts']} pkts ({s['tx_bytes']//1024} KB)  "
            f"auth_fail={s['auth_fail']}"
        )


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="tinyvpn server — AES-256-GCM over UDP",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--key",  required=True,
                        help="32-byte hex key (64 hex chars). "
                             "Generate: python3 -c \"import os; print(os.urandom(32).hex())\"")
    parser.add_argument("--port", type=int, default=8888,
                        help="UDP port to listen on (default: 8888)")
    parser.add_argument("--ip",   default="10.0.0.1",
                        help="TUN interface IP for this machine (default: 10.0.0.1)")
    parser.add_argument("--iface", default="tun0",
                        help="TUN interface name (default: tun0)")
    args = parser.parse_args()

    # ── Validate key ──────────────────────────────────────────────────────────
    try:
        raw_key = bytes.fromhex(args.key)
    except ValueError:
        sys.exit("[!] --key must be a hex string")
    if len(raw_key) != 32:
        sys.exit(f"[!] Key must be exactly 32 bytes (64 hex chars), got {len(raw_key)}")

    aes = AESGCM(raw_key)
    print(f"[*] Key loaded — AES-256-GCM  (first 8 hex: {args.key[:8]}…)")

    # ── TUN device ────────────────────────────────────────────────────────────
    tun_fd = open_tun(args.iface)
    configure_iface(args.iface, args.ip)

    # ── UDP socket ────────────────────────────────────────────────────────────
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", args.port))
    print(f"[*] Listening on UDP 0.0.0.0:{args.port}")
    print(f"[*] Waiting for client…  (share your key with the client operator)")

    # Peer address is discovered from the first incoming datagram
    peer_ref = [None]   # mutable reference updated by udp_to_tun

    # ── Launch threads ────────────────────────────────────────────────────────
    threading.Thread(
        target=stats_printer, args=(5,), daemon=True, name="stats"
    ).start()

    # udp→tun runs in a background thread; tun→udp runs in main thread
    rx_thread = threading.Thread(
        target=udp_to_tun, args=(tun_fd, sock, aes, peer_ref),
        daemon=True, name="udp→tun"
    )
    rx_thread.start()

    # tun→udp needs a peer address; poll until one is known
    print("[*] Waiting for first packet from client to learn peer address…")
    while peer_ref[0] is None:
        time.sleep(0.05)

    print(f"[+] Peer locked in: {peer_ref[0]}  — tunnel is live")
    tun_to_udp(tun_fd, sock, aes, peer_ref[0])


if __name__ == "__main__":
    if os.geteuid() != 0:
        sys.exit("[!] Must be run as root (sudo python3 server.py …)")
    main()
