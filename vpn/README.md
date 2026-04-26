# tinyvpn 🔐

**A working VPN in ~200 lines of Python.** Raw TUN interfaces. AES-256-GCM encryption. Zero dependencies beyond the Python standard library and `cryptography`.

This is the actual mechanism behind every VPN product — WireGuard, OpenVPN, Tailscale — distilled to its essence. Read the source. You'll understand all of them.

```
Machine A (client)                         Machine B (server)
──────────────────                         ──────────────────
 app writes to 10.0.0.2
      │
 kernel routes → tun0
      │
 os.read(tun_fd)  ← raw IP packet
      │
 AES-256-GCM encrypt + random nonce
      │
 UDP sendto() ─────────────────────────► UDP recvfrom()
                   public internet            │
                                         AES-256-GCM decrypt
                                              │
                                         AEAD tag verified ✓
                                              │
                                         os.write(tun_fd)
                                              │
                                         kernel delivers to app
```

---

## How it works

### 1 — The TUN device

A TUN (network TUNnel) interface is a virtual network card entirely in software. When you `os.read(fd)` from it, the kernel hands you a **raw IP packet** — no Ethernet header, no driver overhead. When you `os.write(fd, pkt)` to it, the kernel delivers that packet to whatever process is listening on the destination address.

```python
fd  = os.open("/dev/net/tun", os.O_RDWR)
ifr = struct.pack("16sH14s", b"tun0", IFF_TUN | IFF_NO_PI, b"\x00" * 14)
fcntl.ioctl(fd, TUNSETIFF, ifr)          # register the interface
```

`IFF_NO_PI` strips the 4-byte packet-info prefix the kernel would otherwise add. We get pure IP, nothing else.

### 2 — Reading and writing packets

```python
# Intercept outgoing traffic
packet = os.read(tun_fd, 65535)   # blocks until a packet arrives

# Inject incoming traffic back into the kernel
os.write(tun_fd, decrypted_packet)
```

That's the entire kernel interface. Two syscalls. The rest is crypto and networking.

### 3 — AES-256-GCM encryption

We use **Authenticated Encryption with Associated Data (AEAD)**. AES-GCM gives us:
- **Confidentiality** — the payload is encrypted (AES in counter mode)
- **Integrity** — a 16-byte GHASH authentication tag detects any tampering
- **Authenticity** — only someone with the key can produce a valid tag

```python
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

aes = AESGCM(key)                         # key = 32 random bytes

# Encrypt
nonce      = os.urandom(12)               # 96-bit, never reuse with same key
ciphertext = aes.encrypt(nonce, packet, None)
wire       = nonce + ciphertext           # prepend nonce for the receiver

# Decrypt (raises InvalidTag if tampered — we drop the packet)
packet = aes.decrypt(wire[:12], wire[12:], None)
```

The wire overhead per packet: **28 bytes** (12 nonce + 16 tag).

### 4 — UDP tunnel

We wrap the ciphertext in UDP, not TCP. Why?

- **No head-of-line blocking** — if a packet is lost, TCP would stall; we just miss one frame (the application layer handles retransmission if it cares)
- **No double-ACK** — TCP-over-TCP causes severe congestion collapse; UDP avoids it
- **Simple** — `sendto` / `recvfrom`, that's it

### 5 — Two threads per side

```
main thread:  tun → encrypt → UDP  (blocks on os.read)
thread:       UDP → decrypt → tun  (blocks on recvfrom)
```

Both loops are I/O-bound; the GIL is released during every syscall, so Python threads work perfectly here.

---

## Quick start

### Prerequisites

```bash
# Python 3.10+
pip install cryptography

# Linux: /dev/net/tun is built into the kernel (no setup needed)
# macOS: install TunTap
brew install --cask tuntap
```

### Generate a shared key

```bash
# Run this once — share the output with both sides securely
python3 -c "import os; print(os.urandom(32).hex())"
# → e.g. a3f1c2d4e5b6a7f8... (64 hex chars)
```

### Run the server

```bash
# On the machine with a public IP / open UDP port
sudo python3 server.py --key <YOUR_KEY>

# With options:
sudo python3 server.py --key <YOUR_KEY> --port 9999 --ip 10.0.0.1 --iface tun0
```

### Run the client

```bash
# On any machine that should connect through the tunnel
sudo python3 client.py --server <SERVER_PUBLIC_IP> --key <SAME_KEY>

# With options:
sudo python3 client.py --server 1.2.3.4 --key <KEY> --port 9999 --ip 10.0.0.2
```

### Test the tunnel

```bash
# From the client — this packet travels encrypted through the internet
ping 10.0.0.1

# From the server — reverse direction
ping 10.0.0.2

# iperf3 bandwidth test
iperf3 -s                        # server
iperf3 -c 10.0.0.1               # client — measure tunnel throughput
```

---

## File layout

```
tinyvpn/
├── tun.py      # TUN device helper — open, configure (Linux + macOS)
├── server.py   # Server: listens on UDP, tunnels to/from TUN
├── client.py   # Client: connects to server, tunnels to/from TUN
└── README.md   # You are here
```

---

## Wire format

```
┌────────────┬──────────────────────────────────────────────────────────┐
│  nonce     │  ciphertext + GHASH tag                                  │
│  12 bytes  │  len(plaintext) + 16 bytes                               │
└────────────┴──────────────────────────────────────────────────────────┘
```

A 1500-byte IP packet becomes a 1528-byte UDP payload (1500 + 12 + 16). Well within the typical 65507-byte UDP limit.

---

## Security properties

| Property | Mechanism |
|---|---|
| Confidentiality | AES-256 in GCM mode |
| Integrity | GHASH authentication tag (128-bit) |
| Authenticity | AEAD — only key-holders can produce valid ciphertext |
| Replay resistance | ⚠ Not implemented (see below) |
| Forward secrecy | ⚠ Not implemented (see below) |

### What's missing vs production VPNs

This is a teaching project. Here's what real VPNs add on top:

**Replay attack prevention** — store a sliding window of seen nonces; reject duplicates. WireGuard uses a 2048-bit bitmap.

**Forward secrecy** — perform a Diffie-Hellman key exchange (WireGuard uses Curve25519) so that compromising the long-term key doesn't expose past sessions.

**Key rotation** — derive per-session keys from the DH exchange; rotate periodically.

**Handshake authentication** — the server should prove it holds the key *before* the client sends real traffic (prevents connecting to an impostor).

**MTU handling** — fragment or clamp TCP MSS to avoid IP fragmentation over the tunnel.

**Obfuscation** — traffic looks like UDP noise; no VPN fingerprint. WireGuard has no response to unauthenticated packets at all.

---

## How WireGuard is different

WireGuard implements the same loop but adds:

- **Noise_IKpsk2 handshake** — Curve25519 ECDH, authenticated with long-term static keys, producing ephemeral session keys (forward secrecy)
- **ChaCha20-Poly1305** instead of AES-GCM (faster on CPUs without AES-NI)
- **Silent rejection** of all unauthenticated packets — the server is invisible until you prove you have the key
- **Implemented in the kernel** — the crypto hot path runs in kernel space; context-switch overhead is eliminated

tinyvpn shows you the skeleton. WireGuard is the skeleton with armour, muscles, and a nervous system.

---

## License

MIT — do whatever you want. Learn, fork, break things, build things.
