#!/usr/bin/env python3
"""
client.py — tinyvpn client
──────────────────────────
Run on any machine that should connect through the VPN tunnel.
Must be run as root (TUN device + network config require it).

Usage:
    sudo python3 client.py --server <SERVER_PUBLIC_IP> --key <SAME_KEY_AS_SERVER>

    # Optional flags:
    sudo python3 client.py --server 1.2.3.4 --key $KEY --port 9999 --ip 10.0.0.2
"""

import os
import sys
import socket
import threading
import argparse
import time
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

sys.path.insert(0, os.path.dirname(__file__))
from tun import open_tun, configure_iface


# ── Packet counters ────────────────────────────────────────────────────────────
_stats = {"rx_pkts": 0, "tx_pkts": 0, "auth_fail": 0, "rx_bytes": 0, "tx_bytes": 0}
_stats_lock = threading.Lock()


def _count(key: str, n: int = 1):
    with _stats_lock:
        _stats[key] += n


# ── Core loops ─────────────────────────────────────────────────────────────────

def tun_to_udp(tun_fd: int, sock: socket.socket, aes: AESGCM, peer: tuple):
    """
    Read IP packets from the local TUN, encrypt, and send to server over UDP.

    Identical to the server-side loop — VPN tunnels are symmetric.
    """
    while True:
        try:
            packet = os.read(tun_fd, 65535)
        except OSError as e:
            print(f"[!] TUN read error: {e}")
            break

        nonce      = os.urandom(12)
        ciphertext = aes.encrypt(nonce, packet, None)

        try:
            sock.sendto(nonce + ciphertext, peer)
            _count("tx_pkts")
            _count("tx_bytes", len(packet))
        except OSError as e:
            print(f"[!] UDP send error: {e}")


def udp_to_tun(tun_fd: int, sock: socket.socket, aes: AESGCM):
    """
    Receive UDP datagrams from the server, authenticate+decrypt, inject into TUN.
    """
    while True:
        try:
            data, _ = sock.recvfrom(65535 + 12 + 16)
        except OSError as e:
            print(f"[!] UDP recv error: {e}")
            break

        if len(data) < 13:
            continue

        nonce      = data[:12]
        ciphertext = data[12:]

        try:
            packet = aes.decrypt(nonce, ciphertext, None)
        except Exception:
            _count("auth_fail")
            with _stats_lock:
                if _stats["auth_fail"] % 100 == 1:
                    print(f"[!] Auth failures: {_stats['auth_fail']}")
            continue

        try:
            os.write(tun_fd, packet)
            _count("rx_pkts")
            _count("rx_bytes", len(packet))
        except OSError as e:
            print(f"[!] TUN write error: {e}")


# ── Keep-alive ─────────────────────────────────────────────────────────────────

def keepalive(sock: socket.socket, peer: tuple, interval: int = 10):
    """
    Send a minimal encrypted ping every `interval` seconds so the server knows
    we are still alive and NAT mappings stay open.

    The payload is a single null byte — tiny, but it round-trips through the
    full encrypt/decrypt path so the server can verify the key still matches.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _AESGCM
    # We import aes from closure via the thread target wrapper below
    while True:
        time.sleep(interval)


def keepalive_loop(sock: socket.socket, aes: AESGCM, peer: tuple, interval: int = 10):
    while True:
        time.sleep(interval)
        try:
            nonce = os.urandom(12)
            sock.sendto(nonce + aes.encrypt(nonce, b"\x00", None), peer)
        except OSError:
            pass


# ── Stats ──────────────────────────────────────────────────────────────────────

def stats_printer(interval: int = 5):
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
        description="tinyvpn client — AES-256-GCM over UDP",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--server", required=True,
                        help="Public IP or hostname of the tinyvpn server")
    parser.add_argument("--key",    required=True,
                        help="32-byte hex key (must match server)")
    parser.add_argument("--port",   type=int, default=8888,
                        help="UDP port the server is listening on (default: 8888)")
    parser.add_argument("--ip",     default="10.0.0.2",
                        help="TUN interface IP for this machine (default: 10.0.0.2)")
    parser.add_argument("--iface",  default="tun0",
                        help="TUN interface name (default: tun0)")
    parser.add_argument("--keepalive", type=int, default=10,
                        help="Keep-alive interval in seconds (default: 10)")
    args = parser.parse_args()

    # ── Validate key ──────────────────────────────────────────────────────────
    try:
        raw_key = bytes.fromhex(args.key)
    except ValueError:
        sys.exit("[!] --key must be a hex string")
    if len(raw_key) != 32:
        sys.exit(f"[!] Key must be 32 bytes (64 hex chars), got {len(raw_key)}")

    aes  = AESGCM(raw_key)
    peer = (args.server, args.port)
    print(f"[*] Key loaded — AES-256-GCM  (first 8 hex: {args.key[:8]}…)")
    print(f"[*] Server: {args.server}:{args.port}")

    # ── TUN device ────────────────────────────────────────────────────────────
    tun_fd = open_tun(args.iface)
    configure_iface(args.iface, args.ip)

    # ── UDP socket ────────────────────────────────────────────────────────────
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # No bind() needed — OS assigns an ephemeral port automatically.

    # ── Handshake: tell the server our UDP port ────────────────────────────────
    # We send one encrypted null byte so the server learns our (IP, port) tuple.
    # This also proves we have the correct key before any real traffic flows.
    hs_nonce = os.urandom(12)
    sock.sendto(hs_nonce + aes.encrypt(hs_nonce, b"\x00", None), peer)
    print(f"[*] Handshake sent → server will now route to us")

    # ── Threads ───────────────────────────────────────────────────────────────
    for target, t_args, name in [
        (stats_printer,   (5,),                            "stats"),
        (udp_to_tun,      (tun_fd, sock, aes),             "udp→tun"),
        (keepalive_loop,  (sock, aes, peer, args.keepalive), "keepalive"),
    ]:
        threading.Thread(target=target, args=t_args, daemon=True, name=name).start()

    print(f"[+] Tunnel live!  ping {args.ip.rsplit('.',1)[0]}.1 to test")
    tun_to_udp(tun_fd, sock, aes, peer)   # main thread


if __name__ == "__main__":
    if os.geteuid() != 0:
        sys.exit("[!] Must be run as root (sudo python3 client.py …)")
    main()
