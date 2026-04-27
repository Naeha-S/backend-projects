# tinyvpn v3

`tinyvpn` is now a modular user-space VPN aimed at Windows + Wintun, with a Python control plane and UDP data plane.

It upgrades the earlier prototype in the following ways:

- Noise-style authenticated handshake using `X25519` + `HKDF`
- Ephemeral session keys with forward secrecy
- AES-256-GCM transport with authenticated packet headers
- Anti-replay sliding window
- Multi-hop onion-style route layers
- Fragmentation and reassembly before transport encryption
- Adaptive UDP pacing based on RTT/loss feedback
- Seamless roaming keyed by peer identity and tunnel id
- Keepalive, reconnect, and rekey loops
- FastAPI dashboard backend with WebSocket metrics
- Plugin hooks for send, receive, and connection events
- Automatic route and DNS management
- Optional packet inspection in debug mode

## Layout

```text
vpn/
├── client.py
├── server.py
├── crypto.py                  # compatibility shim
├── tun.py                     # compatibility shim
├── tinyvpn/
│   ├── config.py
│   ├── congestion.py
│   ├── crypto.py
│   ├── dashboard.py
│   ├── dpi.py
│   ├── fragmentation.py
│   ├── keys.py
│   ├── metrics.py
│   ├── node.py
│   ├── plugins.py
│   ├── protocol.py
│   ├── routing.py
│   └── tun.py
└── tinyvpn_interactive_explainer.html
```

## Key Architecture Decisions

### 1. Authenticated key exchange

The old pre-shared key handshake is gone. The new handshake uses static node keys plus ephemeral X25519 keys:

- client proves identity by encrypting its static public key inside the first Noise-style message
- server responds with a fresh ephemeral key
- both sides derive transport keys using HKDF over multiple DH values
- transport keys are directional: initiator to responder and responder to initiator

This keeps AES-256-GCM as the transport cipher while adding forward secrecy and identity-based roaming.

### 2. Packet model

Each encrypted packet carries:

- version
- message type
- flags
- hop count
- tunnel id
- sequence number
- fragmentation metadata

The serialized header is included as AEAD additional authenticated data, so tampering with transport metadata causes decryption failure.

### 3. Multi-hop

Multi-hop forwarding uses encrypted route layers:

- the outer hop decrypts only its layer
- the decrypted payload reveals the next peer id and an inner encrypted wire packet
- the relay forwards that inner packet without learning the final payload

The client can prebuild nested route layers if it has sessions to each hop in the chain.

### 4. Congestion control

The transport uses a small adaptive pacer:

- token-bucket pacing to avoid burst flooding
- RTT feedback from keepalive acknowledgements
- multiplicative backoff on loss/auth failures

It is intentionally simple, but it prevents the raw UDP socket from becoming an unbounded packet firehose.

### 5. Windows operational model

For Windows, `tinyvpn` uses `pytun-pmd3` on top of Wintun/TAP-style device access and automates:

- interface address assignment
- route injection
- optional default route redirection
- DNS changes
- cleanup on shutdown

## Dependencies

- Python 3.10+
- `cryptography`
- `fastapi`
- `uvicorn`
- `pytun-pmd3` on Windows

Install:

```powershell
cd vpn
pip install cryptography fastapi uvicorn pytun-pmd3
```

## Generate keys

```powershell
cd vpn
python -m tinyvpn.keys --private-out keys\client.key --public-out keys\client.pub
python -m tinyvpn.keys --private-out keys\server.key --public-out keys\server.pub
```

The key files are base64-encoded raw X25519 keys.

## Example configs

### Server

```json
{
  "role": "server",
  "node_name": "edge-server",
  "private_key_file": "keys/server.key",
  "listen_host": "0.0.0.0",
  "listen_port": 8888,
  "tun": {
    "name": "TinyVPN",
    "address": "10.44.0.1",
    "netmask": "255.255.255.0",
    "gateway": "10.44.0.1",
    "mtu": 1360,
    "dns_servers": ["1.1.1.1", "8.8.8.8"],
    "redirect_default_route": false,
    "extra_routes": ["10.44.0.0/24"]
  },
  "peers": [
    {
      "name": "client-a",
      "public_key_file": "keys/client.pub",
      "advertised_tun_ip": "10.44.0.2",
      "persistent_keepalive": 15
    }
  ],
  "dashboard": {
    "enabled": true,
    "host": "127.0.0.1",
    "port": 8080
  },
  "debug": {
    "dpi": true,
    "log_level": "INFO"
  },
  "enable_plugins": ["logging"]
}
```

### Client

```json
{
  "role": "client",
  "node_name": "laptop",
  "private_key_file": "keys/client.key",
  "listen_host": "0.0.0.0",
  "listen_port": 0,
  "tun": {
    "name": "TinyVPN",
    "address": "10.44.0.2",
    "netmask": "255.255.255.0",
    "gateway": "10.44.0.1",
    "mtu": 1360,
    "dns_servers": ["1.1.1.1"],
    "redirect_default_route": true,
    "extra_routes": []
  },
  "peers": [
    {
      "name": "entry",
      "host": "203.0.113.10",
      "port": 8888,
      "public_key_file": "keys/server.pub",
      "advertised_tun_ip": "10.44.0.1",
      "persistent_keepalive": 15
    }
  ],
  "route_chain": ["entry"],
  "dashboard": {
    "enabled": true,
    "host": "127.0.0.1",
    "port": 8081
  },
  "debug": {
    "dpi": false,
    "log_level": "INFO"
  },
  "enable_plugins": ["logging", "traffic-shaper"]
}
```

## Run

Server:

```powershell
cd vpn
python server.py --config server.json
```

Client:

```powershell
cd vpn
python client.py --config client.json
```

Dashboard:

- `GET /health`
- `GET /api/snapshot`
- `WS /ws`

Metrics include:

- active peers
- RX/TX counters
- packet loss
- RTT
- rekeys
- errors
- pacing rate

## Notes

- Multi-hop works best with a reduced MTU because each nested hop consumes space.
- The implementation is still user-space Python, so it prioritizes correctness and observability over kernel-level throughput.
- If you want strict production hardening beyond this repo, the next steps would be cookie-based DoS resistance, explicit peer authorization policies, config signing, and broader integration tests with live Wintun adapters.
