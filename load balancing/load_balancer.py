#!/usr/bin/env python3
"""
A cute but surprisingly resilient load balancer built from scratch.
Features:
 - Fixed worker thread pool (no unbounded thread explosions)
 - HTTP/1.1 chunked/streaming support (so it doesn't buffer forever)
 - Prometheus metrics (gotta track that /metrics)
 - Config file, CLI overrides, and a cool terminal UI
 - Graceful shutdown and retry limits
"""

import argparse
import hashlib
import json
import os
import queue
import random
import re
import select
import signal
import socket
import sys
import threading
import time
import uuid
from collections import Counter, deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, List, Optional, Tuple, Any
import urllib.error
import urllib.request

# ANSI colors used by the terminal UI
R = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
PINK_SOFT = "\033[38;5;218m"
PINK = "\033[38;5;213m"
PINK_BRIGHT = "\033[38;5;219m"
ROSE = "\033[38;5;211m"
PEACH = "\033[38;5;223m"
RED = "\033[38;5;203m"
WHITE = "\033[38;5;255m"
GRAY = "\033[38;5;245m"
MINT = "\033[38;5;158m"

# Keep legacy names mapped so non-UI logic remains unchanged.
GREEN = MINT
CYAN = PINK_SOFT
YELLOW = PEACH
MAGENTA = PINK
TEAL = ROSE

STARTUP_BANNER = [
    "##    ##   ####    #######   ##   ##    ####",
    "###   ##  ##  ##   ##        ##   ##   ##  ##",
    "## #  ## ##    ##  #####     #######  ##    ##",
    "##  # ## ########  ##        ##   ##  ########",
    "##   ### ##    ##  ##        ##   ##  ##    ##",
    "##    ## ##    ##  #######   ##   ##  ##    ##",
    "              N A E H A   L O A D   B A L A N C E R",
]

DEFAULT_CONFIG: Dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 8080,
    "algorithm": "round_robin",
    "health_interval": 4.0,
    "health_timeout": 2.0,
    "max_workers": 64,
    "queue_size": 512,
    "connect_timeout": 5.0,
    "read_timeout": 10.0,
    "max_header_kb": 64,
    "shutdown_timeout": 15.0,
    "retry_attempts": 1,
    "backend_fail_threshold": 3,
    "metrics_path": "/metrics",
    "ui_refresh_ms": 250,
    "start_mock_backends": True,
    "max_log_lines": 32,
    "rate_limit_rps": 0.0,
    "rate_limit_burst": 20,
    "circuit_cooldown": 10.0,
    "access_log": "",
    "backends": [
        {"id": "srv-1", "host": "127.0.0.1", "port": 9001, "weight": 3, "color": GREEN},
        {"id": "srv-2", "host": "127.0.0.1", "port": 9002, "weight": 2, "color": CYAN},
        {"id": "srv-3", "host": "127.0.0.1", "port": 9003, "weight": 1, "color": MAGENTA},
    ],
}

RESPONSE_503 = b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Type: text/plain\r\nContent-Length: 20\r\n\r\nNo healthy backends\n"


class TokenBucket:
    """
    Classic token bucket for per-IP rate limiting.
    Each bucket refills at `rate` tokens/sec up to `burst` max tokens.
    A request costs one token; if none left, the request gets rejected.
    """

    def __init__(self, rate: float, burst: int):
        self.rate = rate
        self.burst = burst
        self.tokens = float(burst)
        self.last_refill = time.time()

    def allow(self) -> bool:
        now = time.time()
        elapsed = now - self.last_refill
        self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
        self.last_refill = now

        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class BufferedSocket:
    """
    Wraps an ugly raw socket with a nice read buffer. 
    This prevents us from reading too much from the stream and losing bytes we need later.
    """

    def __init__(self, sock: socket.socket, initial: bytes = b""):
        self.sock = sock
        self.buf = bytearray(initial)

    def recv(self, n: int) -> bytes:
        if self.buf:
            chunk = bytes(self.buf[:n])
            del self.buf[:n]
            return chunk
        return self.sock.recv(n)

    def recv_exact(self, n: int) -> bytes:
        out = bytearray()
        while len(out) < n:
            chunk = self.recv(n - len(out))
            if not chunk:
                raise ConnectionError("unexpected EOF")
            out.extend(chunk)
        return bytes(out)

    def recv_until(self, marker: bytes, max_bytes: int) -> bytes:
        while marker not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            self.buf.extend(chunk)
            if len(self.buf) > max_bytes:
                raise ValueError("header exceeds limit")

        idx = self.buf.find(marker)
        if idx == -1:
            data = bytes(self.buf)
            self.buf.clear()
            return data

        end = idx + len(marker)
        data = bytes(self.buf[:end])
        del self.buf[:end]
        return data


class BackendState:
    def __init__(self, cfg: Dict[str, Any]):
        self.id = str(cfg["id"])
        self.host = str(cfg["host"])
        self.port = int(cfg["port"])
        self.weight = int(cfg.get("weight", 1))
        self.color = str(cfg.get("color", WHITE))

        self.healthy = True
        self.connections = 0
        self.total_requests = 0
        self.total_errors = 0
        self.total_bytes = 0
        self.response_times = deque(maxlen=200)
        self.last_check = "-"  # When we last checked health
        self.consecutive_failures = 0  # Number of times it failed in a row

        # Circuit breaker — tracks whether we should even try this backend
        self.circuit_state = "CLOSED"  # CLOSED = healthy, OPEN = broken, HALF_OPEN = testing
        self.circuit_opened_at = 0.0   # timestamp when we tripped to OPEN


class LoadBalancer:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.backends: Dict[str, BackendState] = {b["id"]: BackendState(b) for b in cfg["backends"]}
        self.backend_ids = list(self.backends.keys())

        self.lock = threading.Lock()
        self.shutdown_event = threading.Event()
        self.accepting = True
        self.rr_index = 0
        self.logs = deque(maxlen=int(cfg["max_log_lines"]))

        self.total_requests = 0
        self.total_errors = 0
        self.start_time = time.time()
        self.req_times = deque(maxlen=5000)
        self.latency_samples_ms = deque(maxlen=5000)
        self.backend_distribution = Counter()
        self.status_distribution = Counter()
        self.method_distribution = Counter()
        self.path_distribution = Counter()
        self.latency_band_distribution = Counter()
        self.last_requests = deque(maxlen=24)
        self.route_trace = deque(maxlen=16)
        self.req_seq = 0

        self.demo_mode = "LIVE"
        self.forced_down = set()
        self.live_rps_history = deque(maxlen=16)
        self.synthetic_qps_target = 0.0

        self.queue: queue.Queue[Tuple[socket.socket, Tuple[str, int]]] = queue.Queue(maxsize=int(cfg["queue_size"]))
        self.in_flight = 0
        self.listener: Optional[socket.socket] = None
        self.worker_threads: List[threading.Thread] = []
        self.mock_servers: Dict[str, HTTPServer] = {}

        # Per-IP rate limiting buckets (only active when rate_limit_rps > 0)
        self.rate_limiters: Dict[str, TokenBucket] = {}

        # Structured JSON access log file handle (None if disabled)
        self.access_log_file = None
        access_log_path = str(cfg.get("access_log", ""))
        if access_log_path:
            try:
                self.access_log_file = open(access_log_path, "a", encoding="utf-8")
            except OSError as e:
                print(f"WARNING: Could not open access log {access_log_path}: {e}")

    # ----------------------------- utility and stats -----------------------------
    def log(self, msg: str, level: str = "INFO") -> None:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:12]
        color = {
            "INFO": PINK_SOFT,
            "OK": MINT,
            "ERR": RED,
            "WARN": PEACH,
            "HIT": WHITE,
            "HEALTH": ROSE,
            "MET": PINK_BRIGHT,
        }.get(level, WHITE)
        with self.lock:
            self.logs.append(f"{GRAY}{ts}{R} {color}{msg}{R}")

    def req_per_sec(self) -> float:
        now = time.time()
        with self.lock:
            recent = [t for t in self.req_times if now - t < 5]
        return len(recent) / 5.0

    @staticmethod
    def _percentile(values: List[float], p: float) -> float:
        if not values:
            return 0.0
        sorted_values = sorted(values)
        idx = int((len(sorted_values) - 1) * p)
        return sorted_values[idx]

    def p50_p95(self) -> Tuple[float, float]:
        with self.lock:
            vals = list(self.latency_samples_ms)
        return self._percentile(vals, 0.50), self._percentile(vals, 0.95)

    @staticmethod
    def normalize_path(path: str) -> str:
        path_only = path.split("?", 1)[0]
        parts = [p for p in path_only.split("/") if p]
        if not parts:
            return "/"
        trimmed = "/" + "/".join(parts[:2])
        return trimmed if len(trimmed) <= 24 else (trimmed[:23] + "~")

    @staticmethod
    def latency_band(latency_ms: float) -> str:
        if latency_ms < 25:
            return "lt25"
        if latency_ms < 75:
            return "25to75"
        if latency_ms < 200:
            return "75to200"
        return "ge200"

    @staticmethod
    def ascii_mini_bar(value: int, max_value: int, width: int = 10) -> str:
        if max_value <= 0:
            return "." * width
        fill = int((value / max_value) * width)
        fill = min(width, max(0, fill))
        return ("#" * fill) + ("." * (width - fill))

    def avg_rt(self, backend: BackendState) -> float:
        vals = list(backend.response_times)
        return sum(vals) / len(vals) if vals else 0.0

    def apply_demo_mode(self, mode: str, manual: bool = False) -> None:
        mode = mode.upper()
        if mode not in {"LIVE", "CHAOS", "STRESS TEST", "FAILOVER"}:
            return

        with self.lock:
            self.demo_mode = mode
            if mode == "LIVE":
                self.synthetic_qps_target = 0.0
                self.forced_down.clear()
            elif mode == "CHAOS":
                self.synthetic_qps_target = 12.0
            elif mode == "STRESS TEST":
                self.synthetic_qps_target = 35.0
                self.forced_down.clear()
            elif mode == "FAILOVER":
                self.synthetic_qps_target = 18.0
                self.forced_down.clear()
                if len(self.backend_ids) >= 2:
                    self.forced_down.add(self.backend_ids[1])

        if manual:
            self.log(f"MODE -> {mode}", "INFO")

    def cycle_demo_mode(self) -> None:
        modes = ["LIVE", "CHAOS", "STRESS TEST", "FAILOVER"]
        with self.lock:
            current = self.demo_mode
        idx = modes.index(current) if current in modes else 0
        self.apply_demo_mode(modes[(idx + 1) % len(modes)], manual=True)

    def toggle_backend_forced(self, idx: int) -> None:
        if idx < 0 or idx >= len(self.backend_ids):
            return
        sid = self.backend_ids[idx]
        with self.lock:
            if sid in self.forced_down:
                self.forced_down.remove(sid)
                forced = False
            else:
                self.forced_down.add(sid)
                forced = True
        if forced:
            self.log(f"manual failover: {sid} -> DOWN", "WARN")
        else:
            self.log(f"manual failover: {sid} -> UP", "OK")

    def live_rate(self, raw_rps: float) -> Tuple[float, str, float]:
        jitter = random.uniform(-0.8, 1.4)
        if random.random() < 0.10:
            jitter += random.uniform(3.5, 10.0)
        with self.lock:
            if self.demo_mode == "STRESS TEST":
                jitter += random.uniform(4.0, 14.0)
            elif self.demo_mode == "CHAOS":
                jitter += random.uniform(-2.0, 8.0)

        live = max(0.0, raw_rps + jitter)
        self.live_rps_history.append(live)
        prev = self.live_rps_history[-2] if len(self.live_rps_history) >= 2 else live
        delta = live - prev
        arrow = "^" if delta > 0.7 else "v" if delta < -0.7 else "-"
        return live, arrow, delta

    def synthetic_load_loop(self) -> None:
        paths = ["/", "/api/data", "/health", "/shop/list", "/v1/items", "/profile/view"]
        while not self.shutdown_event.is_set():
            with self.lock:
                target_qps = self.synthetic_qps_target
                mode = self.demo_mode

            if target_qps <= 0.01:
                time.sleep(0.2)
                continue

            path = random.choice(paths)
            url = f"http://{self.cfg['host']}:{self.cfg['port']}{path}"
            try:
                req = urllib.request.Request(url=url, method="GET")
                with urllib.request.urlopen(req, timeout=1.2) as resp:
                    _ = resp.status
            except Exception:
                pass

            if mode == "CHAOS" and random.random() < 0.03 and self.backend_ids:
                idx = random.randrange(0, len(self.backend_ids))
                self.toggle_backend_forced(idx)

            interval = 1.0 / max(1.0, target_qps)
            if random.random() < 0.12:
                interval *= random.uniform(0.25, 0.60)
            interval *= random.uniform(0.75, 1.35)
            time.sleep(max(0.01, interval))

    # ----------------------------- backend selection -----------------------------
    def pick_server(self, client_ip: str = "") -> Optional[BackendState]:
        now = time.time()
        cooldown = float(self.cfg["circuit_cooldown"])

        with self.lock:
            # First, transition any OPEN circuits that have cooled down to HALF_OPEN
            for b in self.backends.values():
                if b.circuit_state == "OPEN" and (now - b.circuit_opened_at) >= cooldown:
                    b.circuit_state = "HALF_OPEN"

            # Filter: skip forced-down, unhealthy, and OPEN-circuit backends
            healthy = [
                b for sid, b in self.backends.items()
                if b.healthy and sid not in self.forced_down and b.circuit_state != "OPEN"
            ]
            if not healthy:
                return None

            algo = str(self.cfg["algorithm"])

            if algo == "round_robin":
                idx = self.rr_index % len(healthy)
                self.rr_index += 1
                return healthy[idx]

            if algo == "least_conn":
                return min(healthy, key=lambda b: b.connections)

            if algo == "weighted":
                pool = []
                for b in healthy:
                    pool.extend([b] * max(1, b.weight))
                return random.choice(pool)

            if algo == "ip_hash":
                # Consistent hashing on client IP — same client always hits same backend
                digest = int(hashlib.md5(client_ip.encode()).hexdigest(), 16)
                return healthy[digest % len(healthy)]

            return healthy[0]

    # ----------------------------- HTTP parsing helpers -----------------------------
    def parse_headers(self, data: bytes) -> Tuple[str, List[Tuple[str, str]], Dict[str, str]]:
        # Sometimes people send weird encodings, let's gracefully fallback
        text = data.decode("iso-8859-1", errors="replace")
        lines = text.split("\r\n")
        
        start_line = lines[0] if lines else ""
        pairs: List[Tuple[str, str]] = []
        lower_map: Dict[str, str] = {}
        
        for line in lines[1:]:
            if not line:
                continue
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            key = k.strip()
            value = v.strip()
            pairs.append((key, value))
            lower_map[key.lower()] = value
        return start_line, pairs, lower_map

    def rebuild_headers(
        self,
        start_line: str,
        headers: List[Tuple[str, str]],
        overrides: Dict[str, str],
        drop: Optional[List[str]] = None,
    ) -> bytes:
        drop_set = {h.lower() for h in (drop or [])}
        out = [start_line]
        seen = set()
        for k, v in headers:
            lk = k.lower()
            if lk in drop_set:
                continue
            if lk in overrides:
                out.append(f"{k}: {overrides[lk]}")
                seen.add(lk)
            else:
                out.append(f"{k}: {v}")
        for k, v in overrides.items():
            if k not in seen:
                out.append(f"{k}: {v}")
        return ("\r\n".join(out) + "\r\n\r\n").encode("iso-8859-1")

    def stream_content_length(self, src: BufferedSocket, dst: socket.socket, initial: bytes, length: int) -> int:
        sent = 0
        remaining = length
        if initial:
            chunk = initial[:remaining]
            if chunk:
                dst.sendall(chunk)
                sent += len(chunk)
                remaining -= len(chunk)
        while remaining > 0:
            chunk = src.recv(min(65536, remaining))
            if not chunk:
                raise ConnectionError("unexpected EOF while reading fixed-length body")
            dst.sendall(chunk)
            sent += len(chunk)
            remaining -= len(chunk)
        return sent

    def stream_chunked(self, src: BufferedSocket, dst: socket.socket, initial: bytes) -> int:
        sent = 0
        if initial:
            src.buf = bytearray(initial) + src.buf

        while True:
            line = src.recv_until(b"\r\n", 8192)
            if not line.endswith(b"\r\n"):
                raise ConnectionError("invalid chunk framing")
            dst.sendall(line)
            sent += len(line)

            line_text = line[:-2].decode("ascii", errors="replace")
            size_hex = line_text.split(";", 1)[0].strip()
            size = int(size_hex, 16)

            if size == 0:
                # Consume trailers up to CRLF CRLF
                trailers = src.recv_until(b"\r\n\r\n", 65536)
                dst.sendall(trailers)
                sent += len(trailers)
                return sent

            data = src.recv_exact(size + 2)
            dst.sendall(data)
            sent += len(data)

    def drain_to_close(self, src: BufferedSocket, dst: socket.socket, initial: bytes = b"") -> int:
        sent = 0
        if initial:
            dst.sendall(initial)
            sent += len(initial)
        while True:
            chunk = src.recv(65536)
            if not chunk:
                break
            dst.sendall(chunk)
            sent += len(chunk)
        return sent

    # ----------------------------- observability -----------------------------
    def prom_metrics(self) -> str:
        with self.lock:
            total_requests = self.total_requests
            total_errors = self.total_errors
            queue_depth = self.queue.qsize()
            in_flight = self.in_flight
            req_times = list(self.req_times)
            lat_samples = list(self.latency_samples_ms)
            backend_dist = dict(self.backend_distribution)
            status_dist = dict(self.status_distribution)
            method_dist = dict(self.method_distribution)
            path_dist = dict(self.path_distribution)
            latency_bands = dict(self.latency_band_distribution)
            backends = list(self.backends.values())

        now = time.time()
        recent_1m = [t for t in req_times if now - t < 60]
        req_rate = len(recent_1m) / 60.0

        p50 = self._percentile(lat_samples, 0.5)
        p95 = self._percentile(lat_samples, 0.95)

        buckets = [5, 10, 25, 50, 100, 250, 500, 1000, 2000, 5000]
        bucket_counts = []
        for b in buckets:
            bucket_counts.append(sum(1 for v in lat_samples if v <= b))

        lines = []
        lines.append("# HELP lb_requests_total Total number of requests accepted by the load balancer")
        lines.append("# TYPE lb_requests_total counter")
        lines.append(f"lb_requests_total {total_requests}")

        lines.append("# HELP lb_errors_total Total number of failed requests")
        lines.append("# TYPE lb_errors_total counter")
        lines.append(f"lb_errors_total {total_errors}")

        lines.append("# HELP lb_in_flight Current number of in-flight requests")
        lines.append("# TYPE lb_in_flight gauge")
        lines.append(f"lb_in_flight {in_flight}")

        lines.append("# HELP lb_queue_depth Current request queue depth")
        lines.append("# TYPE lb_queue_depth gauge")
        lines.append(f"lb_queue_depth {queue_depth}")

        lines.append("# HELP lb_request_rate_per_second_1m Requests per second over rolling 1 minute")
        lines.append("# TYPE lb_request_rate_per_second_1m gauge")
        lines.append(f"lb_request_rate_per_second_1m {req_rate:.4f}")

        lines.append("# HELP lb_request_latency_ms p50 request latency in milliseconds")
        lines.append("# TYPE lb_request_latency_ms gauge")
        lines.append(f"lb_request_latency_ms{{quantile=\"0.50\"}} {p50:.3f}")
        lines.append(f"lb_request_latency_ms{{quantile=\"0.95\"}} {p95:.3f}")

        lines.append("# HELP lb_request_latency_ms_bucket Request latency histogram buckets")
        lines.append("# TYPE lb_request_latency_ms_bucket histogram")
        for b, c in zip(buckets, bucket_counts):
            lines.append(f"lb_request_latency_ms_bucket{{le=\"{b}\"}} {c}")
        lines.append(f"lb_request_latency_ms_bucket{{le=\"+Inf\"}} {len(lat_samples)}")
        lines.append(f"lb_request_latency_ms_count {len(lat_samples)}")
        lines.append(f"lb_request_latency_ms_sum {sum(lat_samples):.3f}")

        lines.append("# HELP lb_backend_requests_total Requests proxied to each backend")
        lines.append("# TYPE lb_backend_requests_total counter")
        for sid, count in sorted(backend_dist.items()):
            lines.append(f"lb_backend_requests_total{{backend=\"{sid}\"}} {count}")

        lines.append("# HELP lb_http_status_total Response status counts")
        lines.append("# TYPE lb_http_status_total counter")
        for status, count in sorted(status_dist.items()):
            lines.append(f"lb_http_status_total{{status=\"{status}\"}} {count}")

        lines.append("# HELP lb_http_method_total Request method counts")
        lines.append("# TYPE lb_http_method_total counter")
        for method, count in sorted(method_dist.items()):
            lines.append(f"lb_http_method_total{{method=\"{method}\"}} {count}")

        lines.append("# HELP lb_http_path_bucket_total Normalized path distribution")
        lines.append("# TYPE lb_http_path_bucket_total counter")
        for pth, count in sorted(path_dist.items()):
            safe = pth.replace('"', "'")
            lines.append(f"lb_http_path_bucket_total{{path=\"{safe}\"}} {count}")

        lines.append("# HELP lb_latency_band_total Request latency band counts")
        lines.append("# TYPE lb_latency_band_total counter")
        for band, count in sorted(latency_bands.items()):
            lines.append(f"lb_latency_band_total{{band=\"{band}\"}} {count}")

        lines.append("# HELP lb_backend_health Backend health state (1 healthy, 0 unhealthy)")
        lines.append("# TYPE lb_backend_health gauge")
        for b in backends:
            lines.append(f"lb_backend_health{{backend=\"{b.id}\"}} {1 if b.healthy else 0}")

        return "\n".join(lines) + "\n"

    # ----------------------------- request handling -----------------------------
    def send_simple_response(self, client: socket.socket, code: int, body: bytes, req_id: str) -> None:
        reason = {
            200: "OK",
            400: "Bad Request",
            429: "Too Many Requests",
            500: "Internal Server Error",
            502: "Bad Gateway",
            503: "Service Unavailable",
        }.get(code, "OK")
        headers = [
            f"HTTP/1.1 {code} {reason}",
            "Connection: close",
            "Content-Type: text/plain",
            f"Content-Length: {len(body)}",
            f"X-Request-ID: {req_id}",
            "",
            "",
        ]
        
        # Finally, send it out to the client
        client.sendall("\r\n".join(headers).encode("iso-8859-1") + body)

    def mark_backend_failure(self, backend: BackendState, reason: str) -> None:
        with self.lock:
            backend.total_errors += 1
            backend.consecutive_failures += 1
            fail_threshold = int(self.cfg["backend_fail_threshold"])
            if backend.consecutive_failures >= fail_threshold:
                backend.healthy = False

                # Trip the circuit breaker to OPEN so we stop sending traffic
                if backend.circuit_state != "OPEN":
                    backend.circuit_state = "OPEN"
                    backend.circuit_opened_at = time.time()
                    self.log(f"CIRCUIT {backend.id} -> OPEN (tripped after {backend.consecutive_failures} failures)", "WARN")

            self.total_errors += 1
        self.log(f"{backend.id} failure ({backend.consecutive_failures}/{self.cfg['backend_fail_threshold']}): {reason}", "ERR")

    def connect_backend(self, backend: BackendState) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(float(self.cfg["connect_timeout"]))
        s.connect((backend.host, backend.port))
        s.settimeout(float(self.cfg["read_timeout"]))
        return s

    def proxy_once(
        self,
        client: socket.socket,
        client_addr: Tuple[str, int],
        req_start_line: str,
        req_headers: List[Tuple[str, str]],
        req_header_map: Dict[str, str],
        req_initial_body: bytes,
        req_id: str,
        method: str,
        path: str,
    ) -> Tuple[str, Optional[str], int]:
        backend = self.pick_server(client_ip=client_addr[0])
        if not backend:
            self.send_simple_response(client, 503, b"No healthy backends\n", req_id)
            return "503", None, 0

        with self.lock:
            backend.connections += 1
            backend.total_requests += 1
            self.backend_distribution[backend.id] += 1

        backend_sock = None
        total_bytes = 0
        status_code = "502"
        try:
            backend_sock = self.connect_backend(backend)
            bsrc = BufferedSocket(backend_sock)

            overrides = {
                "x-forwarded-for": client_addr[0],
                "x-forwarded-proto": "http",
                "x-request-id": req_id,
                "x-lb-backend": backend.id,
                "connection": "close",
            }
            rebuilt_req_headers = self.rebuild_headers(
                req_start_line,
                req_headers,
                overrides=overrides,
                drop=["proxy-connection", "connection"],
            )
            backend_sock.sendall(rebuilt_req_headers)

            transfer_encoding = req_header_map.get("transfer-encoding", "").lower()
            content_length = req_header_map.get("content-length")
            if "chunked" in transfer_encoding:
                csrc = BufferedSocket(client, req_initial_body)
                self.stream_chunked(csrc, backend_sock, initial=b"")
            elif content_length is not None:
                length = int(content_length)
                csrc = BufferedSocket(client)
                self.stream_content_length(csrc, backend_sock, req_initial_body, length)
            elif req_initial_body:
                backend_sock.sendall(req_initial_body)

            raw_resp_headers = bsrc.recv_until(b"\r\n\r\n", int(self.cfg["max_header_kb"]) * 1024)
            if b"\r\n\r\n" not in raw_resp_headers:
                raise ConnectionError("incomplete response headers")

            resp_header_bytes = raw_resp_headers[:-4]
            resp_initial_body = bytes(bsrc.buf)
            bsrc.buf.clear()
            resp_start_line, resp_headers, resp_map = self.parse_headers(resp_header_bytes)
            parts = resp_start_line.split(" ")
            if len(parts) >= 2:
                status_code = parts[1]

            resp_overrides = {
                "x-request-id": req_id,
                "connection": "close",
            }
            rebuilt_resp_headers = self.rebuild_headers(
                resp_start_line,
                resp_headers,
                overrides=resp_overrides,
                drop=["proxy-connection", "connection"],
            )
            client.sendall(rebuilt_resp_headers)
            total_bytes += len(rebuilt_resp_headers)

            resp_transfer_encoding = resp_map.get("transfer-encoding", "").lower()
            resp_content_length = resp_map.get("content-length")

            if method.upper() == "HEAD":
                pass
            elif "chunked" in resp_transfer_encoding:
                total_bytes += self.stream_chunked(bsrc, client, resp_initial_body)
            elif resp_content_length is not None:
                total_bytes += self.stream_content_length(bsrc, client, resp_initial_body, int(resp_content_length))
            else:
                total_bytes += self.drain_to_close(bsrc, client, resp_initial_body)

            with self.lock:
                backend.total_bytes += total_bytes
                backend.consecutive_failures = 0

                # If the circuit was HALF_OPEN and we succeeded, close it back up
                if backend.circuit_state == "HALF_OPEN":
                    backend.circuit_state = "CLOSED"
                    self.log(f"CIRCUIT {backend.id} -> CLOSED (recovered)", "OK")

            return status_code, backend.id, total_bytes
        except Exception as exc:
            self.mark_backend_failure(backend, str(exc)[:80])
            raise
        finally:
            with self.lock:
                backend.connections = max(0, backend.connections - 1)
            if backend_sock:
                try:
                    backend_sock.close()
                except OSError:
                    pass

    def handle_client(self, client: socket.socket, client_addr: Tuple[str, int]) -> None:
        t_start = time.time()
        req_id = uuid.uuid4().hex[:12]
        status_code = "500"
        selected_backend: Optional[str] = None
        total_bytes = 0

        with self.lock:
            self.in_flight += 1

        try:
            client.settimeout(float(self.cfg["read_timeout"]))
            csrc = BufferedSocket(client)

            raw_headers = csrc.recv_until(b"\r\n\r\n", int(self.cfg["max_header_kb"]) * 1024)
            if b"\r\n\r\n" not in raw_headers:
                self.send_simple_response(client, 400, b"Invalid request\n", req_id)
                status_code = "400"
                return

            req_header_bytes = raw_headers[:-4]
            req_initial_body = bytes(csrc.buf)
            csrc.buf.clear()
            req_start_line, req_headers, req_map = self.parse_headers(req_header_bytes)
            if not req_start_line:
                self.send_simple_response(client, 400, b"Invalid request line\n", req_id)
                status_code = "400"
                return

            parts = req_start_line.split(" ")
            if len(parts) < 2:
                self.send_simple_response(client, 400, b"Invalid request line\n", req_id)
                status_code = "400"
                return

            method = parts[0]
            path = parts[1]
            path_bucket = self.normalize_path(path)

            # ---- Per-IP rate limiting (skip if disabled) ----
            rps_limit = float(self.cfg["rate_limit_rps"])
            if rps_limit > 0:
                ip = client_addr[0]
                with self.lock:
                    if ip not in self.rate_limiters:
                        self.rate_limiters[ip] = TokenBucket(rps_limit, int(self.cfg["rate_limit_burst"]))
                    bucket = self.rate_limiters[ip]
                if not bucket.allow():
                    self.send_simple_response(client, 429, b"Rate limit exceeded\n", req_id)
                    status_code = "429"
                    with self.lock:
                        self.total_errors += 1
                        self.status_distribution["429"] += 1
                    self.log(f"req={req_id} rate limited {ip}", "WARN")
                    return

            with self.lock:
                self.total_requests += 1
                self.req_times.append(time.time())
                self.method_distribution[method.upper()] += 1
                self.path_distribution[path_bucket] += 1
                self.req_seq += 1
                req_num = self.req_seq

            if method.upper() == "GET" and path == str(self.cfg["metrics_path"]):
                body = self.prom_metrics().encode("utf-8")
                headers = [
                    "HTTP/1.1 200 OK",
                    "Connection: close",
                    "Content-Type: text/plain; version=0.0.4",
                    f"Content-Length: {len(body)}",
                    f"X-Request-ID: {req_id}",
                    "",
                    "",
                ]
                client.sendall("\r\n".join(headers).encode("iso-8859-1") + body)
                status_code = "200"
                self.log(f"req={req_id} METRICS {method} {path}", "MET")
                return

            if method.upper() == "GET" and path == "/lb/status":
                with self.lock:
                    payload = {
                        "name": "NAEHA Load Balancer",
                        "uptime_s": int(time.time() - self.start_time),
                        "algorithm": self.cfg["algorithm"],
                        "requests_total": self.total_requests,
                        "errors_total": self.total_errors,
                        "queue_depth": self.queue.qsize(),
                        "in_flight": self.in_flight,
                        "p50_ms": round(self._percentile(list(self.latency_samples_ms), 0.50), 2),
                        "p95_ms": round(self._percentile(list(self.latency_samples_ms), 0.95), 2),
                        "mode": self.demo_mode,
                        "forced_down": sorted(self.forced_down),
                        "methods": dict(self.method_distribution),
                        "status": dict(self.status_distribution),
                        "paths": dict(self.path_distribution.most_common(8)),
                        "route_trace": list(self.route_trace)[-8:],
                        "backends": {
                            b.id: {
                                "healthy": b.healthy,
                                "connections": b.connections,
                                "requests": b.total_requests,
                                "errors": b.total_errors,
                                "avg_ms": round(self.avg_rt(b), 2),
                            }
                            for b in self.backends.values()
                        },
                    }
                body = json.dumps(payload, indent=2).encode("utf-8")
                headers = [
                    "HTTP/1.1 200 OK",
                    "Connection: close",
                    "Content-Type: application/json",
                    f"Content-Length: {len(body)}",
                    f"X-Request-ID: {req_id}",
                    "",
                    "",
                ]
                client.sendall("\r\n".join(headers).encode("iso-8859-1") + body)
                status_code = "200"
                self.log(f"req={req_id} STATUS {method} {path}", "MET")
                return

            if method.upper() == "GET" and path == "/lb/health":
                self.send_simple_response(client, 200, b"OK\n", req_id)
                status_code = "200"
                return

            attempts = 1 + int(self.cfg["retry_attempts"])
            for attempt in range(1, attempts + 1):
                try:
                    status_code, selected_backend, total_bytes = self.proxy_once(
                        client,
                        client_addr,
                        req_start_line,
                        req_headers,
                        req_map,
                        req_initial_body,
                        req_id,
                        method,
                        path,
                    )
                    break
                except Exception:
                    if attempt >= attempts:
                        self.send_simple_response(client, 502, b"Backend error\n", req_id)
                        status_code = "502"
                        break

            elapsed_ms = (time.time() - t_start) * 1000.0
            with self.lock:
                self.latency_samples_ms.append(elapsed_ms)
                self.status_distribution[status_code] += 1
                self.latency_band_distribution[self.latency_band(elapsed_ms)] += 1
                self.last_requests.append(
                    {
                        "num": req_num,
                        "id": req_id,
                        "method": method,
                        "path": path_bucket,
                        "status": status_code,
                        "backend": selected_backend or "-",
                        "ms": round(elapsed_ms, 1),
                    }
                )
                self.route_trace.append({"num": req_num, "backend": selected_backend or "-", "status": status_code})
                if selected_backend and selected_backend in self.backends:
                    self.backends[selected_backend].response_times.append(elapsed_ms)

            backend_label = selected_backend or "-"
            color = self.backends[selected_backend].color if selected_backend in self.backends else WHITE
            st_color = GREEN if status_code.startswith("2") else YELLOW if status_code.startswith("3") else RED
            self.log(
                f"REQ#{req_num} req={req_id} {color}{backend_label}{R} {st_color}{status_code}{R} {method} {path} {GRAY}{elapsed_ms:.1f}ms {total_bytes}b{R}",
                "HIT",
            )

            # ---- Structured JSON access log ----
            if self.access_log_file:
                log_entry = json.dumps({
                    "ts": datetime.now().isoformat(),
                    "req_id": req_id,
                    "method": method,
                    "path": path,
                    "status": int(status_code) if status_code.isdigit() else 0,
                    "backend": selected_backend or "-",
                    "latency_ms": round(elapsed_ms, 2),
                    "client_ip": client_addr[0],
                    "bytes": total_bytes,
                })
                try:
                    self.access_log_file.write(log_entry + "\n")
                    self.access_log_file.flush()
                except OSError:
                    pass

        except Exception as exc:
            with self.lock:
                self.total_errors += 1
                self.status_distribution["500"] += 1
            self.log(f"req={req_id} handler error: {str(exc)[:80]}", "ERR")
            try:
                self.send_simple_response(client, 500, b"Internal error\n", req_id)
            except OSError:
                pass
        finally:
            with self.lock:
                self.in_flight = max(0, self.in_flight - 1)
            try:
                client.close()
            except OSError:
                pass

    # ----------------------------- bounded concurrency -----------------------------
    def worker_loop(self) -> None:
        while not self.shutdown_event.is_set() or not self.queue.empty():
            try:
                client, addr = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                self.handle_client(client, addr)
            finally:
                self.queue.task_done()

    def start_workers(self) -> None:
        worker_count = int(self.cfg["max_workers"])
        for i in range(worker_count):
            t = threading.Thread(target=self.worker_loop, daemon=True, name=f"worker-{i+1}")
            t.start()
            self.worker_threads.append(t)
        self.log(f"Worker pool started with {worker_count} workers", "INFO")

    def start_listener(self) -> None:
        lb = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lb.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        lb.bind((str(self.cfg["host"]), int(self.cfg["port"])))
        lb.listen(256)
        lb.settimeout(1.0)
        self.listener = lb
        self.log(f"Load balancer listening on {self.cfg['host']}:{self.cfg['port']}", "INFO")

        while not self.shutdown_event.is_set():
            try:
                client, addr = lb.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            if not self.accepting:
                try:
                    client.sendall(RESPONSE_503)
                    client.close()
                except OSError:
                    pass
                continue

            try:
                self.queue.put_nowait((client, addr))
            except queue.Full:
                req_id = uuid.uuid4().hex[:12]
                with self.lock:
                    self.total_errors += 1
                    self.status_distribution["429"] += 1
                try:
                    self.send_simple_response(client, 429, b"Load balancer queue full\n", req_id)
                except OSError:
                    pass
                try:
                    client.close()
                except OSError:
                    pass
                self.log(f"req={req_id} queue full -> 429", "WARN")

    # ----------------------------- health checking -----------------------------
    def check_backend_health(self, b: BackendState) -> bool:
        url = f"http://{b.host}:{b.port}/health"
        try:
            req = urllib.request.urlopen(url, timeout=float(self.cfg["health_timeout"]))
            return req.status == 200
        except Exception:
            return False

    def health_loop(self) -> None:
        while not self.shutdown_event.is_set():
            interval = float(self.cfg["health_interval"])
            if self.shutdown_event.wait(interval):
                return

            cooldown = float(self.cfg["circuit_cooldown"])
            now = time.time()

            for b in self.backends.values():
                was_healthy = b.healthy
                now_healthy = self.check_backend_health(b)
                if b.id in self.forced_down:
                    now_healthy = False

                with self.lock:
                    b.healthy = now_healthy
                    b.last_check = datetime.now().strftime("%H:%M:%S")

                    if now_healthy:
                        b.consecutive_failures = 0

                        # If the circuit was OPEN or HALF_OPEN and health check passes, heal it
                        if b.circuit_state in ("OPEN", "HALF_OPEN"):
                            b.circuit_state = "CLOSED"
                            self.log(f"CIRCUIT {b.id} -> CLOSED (health recovered)", "OK")
                    else:
                        # Transition OPEN -> HALF_OPEN after cooldown period
                        if b.circuit_state == "OPEN" and (now - b.circuit_opened_at) >= cooldown:
                            b.circuit_state = "HALF_OPEN"
                            self.log(f"CIRCUIT {b.id} -> HALF_OPEN (cooldown expired, will test)", "INFO")

                if was_healthy != now_healthy:
                    label = f"{GREEN}RECOVERED{R}" if now_healthy else f"{RED}DOWN{R}"
                    self.log(f"Health: {b.id} -> {label}", "HEALTH")

    # ----------------------------- terminal UI -----------------------------
    def get_term_size(self) -> Tuple[int, int]:
        try:
            cols, rows = os.get_terminal_size()
            # Return slightly smaller dimensions so we never hit the bottom/right edges, which triggers terminal auto-scrolling
            return max(80, cols - 1), max(20, rows - 1)
        except OSError:
            return 149, 39

    def health_bar(self, backend: BackendState) -> str:
        total = backend.total_requests
        errs = backend.total_errors
        ok = total - errs
        width = 8
        if total == 0:
            return GRAY + ("-" * width) + R
        ratio = ok / total
        fill = int(ratio * width)
        col = MINT if ratio > 0.9 else PEACH if ratio > 0.7 else RED
        return col + ("#" * fill) + GRAY + ("." * (width - fill)) + R

    @staticmethod
    def _visible_len(text: str) -> int:
        return len(re.sub(r"\x1b\[[0-9;]*m", "", text))

    @staticmethod
    def _clip_ansi(text: str, max_visible: int) -> str:
        if max_visible <= 0:
            return ""

        tokens = re.split(r"(\x1b\[[0-9;]*m)", text)
        out = []
        visible = 0
        clipped = False

        for tok in tokens:
            if not tok:
                continue
            if re.fullmatch(r"\x1b\[[0-9;]*m", tok):
                out.append(tok)
                continue

            remaining = max_visible - visible
            if remaining <= 0:
                clipped = True
                break

            if len(tok) <= remaining:
                out.append(tok)
                visible += len(tok)
            else:
                slice_len = max(0, remaining - 1)
                out.append(tok[:slice_len] + "…")
                visible += min(len(tok), remaining)
                clipped = True
                break

        if clipped and (not out or out[-1] != R):
            out.append(R)
        return "".join(out)

    def ascii_header_lines(self, width: int) -> List[str]:
        out = []
        for idx, line in enumerate(STARTUP_BANNER):
            color = PINK_BRIGHT if idx in (0, 5) else PINK_SOFT
            out.append(f"  {color}{BOLD}{line[:max(0, width - 4)]}{R}")
        return out

    def render_ui(self) -> str:
        w, h = self.get_term_size()
        left_w = w // 2 - 2
        right_w = w - left_w - 3

        with self.lock:
            logs = list(self.logs)
            total_r = self.total_requests
            total_e = self.total_errors
            in_flight = self.in_flight
            queue_depth = self.queue.qsize()
            algo = str(self.cfg["algorithm"])
            mode = self.demo_mode
            forced_down = sorted(self.forced_down)
            backends = list(self.backends.values())
            status_dist = dict(self.status_distribution)
            backend_dist = dict(self.backend_distribution)
            method_dist = dict(self.method_distribution)
            path_dist = dict(self.path_distribution)
            band_dist = dict(self.latency_band_distribution)
            recent = list(self.last_requests)
            routes = list(self.route_trace)
            req_seq = self.req_seq

        p50, p95 = self.p50_p95()
        rps = self.req_per_sec()
        live_rps, trend_arrow, trend_delta = self.live_rate(rps)
        uptime = int(time.time() - self.start_time)
        hh, mm, ss = uptime // 3600, (uptime % 3600) // 60, uptime % 60

        lines = []
        lines.extend(self.ascii_header_lines(w))
        lines.append(
            f"  {PINK_BRIGHT}{BOLD}NAEHA MODE: {mode}{R}  {GRAY}|{R} algo {PINK}{algo.upper()}{R}  {GRAY}|{R} :{self.cfg['port']}  {GRAY}|{R} up {hh:02d}:{mm:02d}:{ss:02d}"
        )
        lines.append(f"{GRAY}{'-' * w}{R}")
        lines.append(f"  {PINK_SOFT}{BOLD}{'LIVE REQUEST STORY':<{left_w}}{R}  {GRAY}|{R}  {ROSE}{BOLD}{'PERFORMANCE + VIBE':>{right_w-2}}{R}")
        lines.append(f"{GRAY}{'-' * w}{R}")

        right = []
        right.append(f"{PINK_BRIGHT}{BOLD}  OVERVIEW (PRO){R}")
        right.append(
            f"  req_total={PINK}{total_r}{R}  req_live={PINK_BRIGHT}{req_seq}{R}  err={RED}{total_e}{R}"
        )
        right.append(f"  rate={MINT}{live_rps:.1f}/s{R} {trend_arrow} ({trend_delta:+.1f})  p50={MINT}{p50:.1f}ms{R}  p95={PEACH}{p95:.1f}ms{R}")
        right.append(f"  inflight={PINK_SOFT}{in_flight}{R}  queue={PINK_SOFT}{queue_depth}{R}  forced_down={RED}{','.join(forced_down) if forced_down else '-'}{R}")

        worker_count = max(1, int(self.cfg["max_workers"]))
        queue_cap = max(1, int(self.cfg["queue_size"]))
        worker_util = min(100.0, (in_flight / worker_count) * 100.0)
        queue_util = min(100.0, (queue_depth / queue_cap) * 100.0)
        right.append(
            f"  workers={worker_count} util={MINT}{worker_util:>5.1f}%{R}  queue_cap={queue_cap} fill={PEACH}{queue_util:>5.1f}%{R}"
        )

        if total_r > 0:
            err_rate = (total_e / total_r) * 100.0
            right.append(f"  error_rate={PEACH}{err_rate:.2f}%{R}  success={MINT}{100.0 - err_rate:.2f}%{R}")
        else:
            right.append(f"  error_rate={PEACH}0.00%{R}  success={MINT}0.00%{R}")

        if status_dist:
            top_status = sorted(status_dist.items(), key=lambda x: x[1], reverse=True)[:3]
            status_line = "  status: " + "  ".join(f"{s}:{c}" for s, c in top_status)
            right.append(f"{GRAY}{status_line}{R}")

        if backend_dist:
            top_backend = sorted(backend_dist.items(), key=lambda x: x[1], reverse=True)[:3]
            backend_line = "  split: " + "  ".join(f"{sid}:{count}" for sid, count in top_backend)
            right.append(f"{GRAY}{backend_line}{R}")

        if method_dist:
            top_methods = sorted(method_dist.items(), key=lambda x: x[1], reverse=True)[:4]
            method_line = "  methods: " + "  ".join(f"{m}:{c}" for m, c in top_methods)
            right.append(f"{GRAY}{method_line}{R}")

        if band_dist:
            band_order = ["lt25", "25to75", "75to200", "ge200"]
            max_band = max([band_dist.get(k, 0) for k in band_order] + [1])
            band_parts = []
            for band in band_order:
                val = band_dist.get(band, 0)
                band_parts.append(f"{band}:{self.ascii_mini_bar(val, max_band, 6)}")
            right.append(f"{GRAY}  latency: {'  '.join(band_parts)}{R}")

        if path_dist:
            top_paths = sorted(path_dist.items(), key=lambda x: x[1], reverse=True)[:3]
            right.append(f"{PINK_BRIGHT}{BOLD}  HOT PATHS{R}")
            for pth, count in top_paths:
                right.append(f"  {PINK_SOFT}{pth:<20}{R} {WHITE}{count}{R}")
        if routes:
            right.append("")
            right.append(f"{PINK_BRIGHT}{BOLD}  ROUTING VISUAL{R}")
            for item in routes[-4:][::-1]:
                right.append(f"  REQ #{item['num']:<4} -> {item['backend']:<6} [{item['status']}]")
            latest = routes[-1]
            right.append(f"  [client] -> [LB] -> [{latest['backend']}]  req#{latest['num']}")
        right.append("")

        right.append(f"{PINK_BRIGHT}{BOLD}  BACKENDS (PRO){R}")
        right.append(f"  {GRAY}{'ID':<7} {'ST':<3} {'REQ':<6} {'ERR':<5} {'CONN':<5} {'AVG':<8} {'CIRCUIT'}{R}")
        right.append(f"  {GRAY}{'-' * (right_w - 2)}{R}")

        for b in backends:
            hc = f"{MINT}o{R}" if b.healthy else f"{RED}x{R}"
            art = self.avg_rt(b)
            rt_col = MINT if art < 30 else PEACH if art < 80 else RED
            
            c_state = b.circuit_state
            c_col = MINT if c_state == "CLOSED" else RED if c_state == "OPEN" else PEACH
            
            right.append(
                f"  {b.color}{BOLD}{b.id:<7}{R} {hc}  {PINK}{b.total_requests:<6}{R} {RED}{b.total_errors:<5}{R} "
                f"{PINK_SOFT}{b.connections:<5}{R} {rt_col}{art:>5.1f}ms{R}  {c_col}{c_state}{R}"
            )
            right.append(f"  {GRAY}  wt:{b.weight}  last:{b.last_check}  bytes:{b.total_bytes/1024:.1f}KB  fails:{b.consecutive_failures}{R}")
        right.append("")
        right.append(f"{PINK_BRIGHT}{BOLD}  CONTROLS (VIBE){R}")
        right.append(f"  {GRAY}keys: [1] rr [2] least [3] weighted [4] ip hash [m] mode [q] quit{R}")
        right.append(f"  {GRAY}failover: [7] srv-1 [8] srv-2 [9] srv-3 toggle DOWN/UP{R}")
        right.append(f"  {GRAY}load: [o] overload burst on/off{R}")
        right.append(f"  {GRAY}metrics: GET {self.cfg['metrics_path']} | health: GET /lb/health{R}")
        right.append(f"  {GRAY}status json: GET /lb/status{R}")
        right.append(f"  {PINK_SOFT}theme: 50/50 cute + production | movement proves routing{R}")

        right.append("")
        right.append(f"{PINK_BRIGHT}{BOLD}  LAST REQUESTS{R}")
        for req in recent[-5:][::-1]:
            right.append(
                f"  #{req.get('num', 0):<4} {req['status']:<3} {req['method']:<4} {req['path']:<14} {req['ms']:>6.1f}ms {req['backend']}"
            )

        # Keep content area adaptive now that we have a multi-line ASCII header.
        content_h = max(10, h - len(lines) - 3)
        visible_logs = logs[max(0, len(logs) - content_h):]
        
        empty_lines = content_h - len(visible_logs)
        art_lines = [
            f" {PINK_SOFT}.  *  .  . *       *    . {R}",
            f" {PINK_BRIGHT}*   AWAITING TRAFFIC    * {R}",
            f" {PINK_SOFT}.  *    *   .      *   .  {R}",
            f" {GRAY}    (curl :8080)          {R}",
        ]
        start_art = (empty_lines - len(art_lines)) // 2

        for i in range(content_h):
            if i < len(visible_logs):
                left_line = visible_logs[i]
            else:
                empty_row = i - len(visible_logs)
                if empty_lines >= len(art_lines) and start_art <= empty_row < start_art + len(art_lines):
                    left_line = art_lines[empty_row - start_art]
                else:
                    left_line = ""

            right_line = right[i] if i < len(right) else ""

            left_clip = self._clip_ansi(left_line, max(12, left_w - 2))
            right_clip = self._clip_ansi(right_line, max(12, right_w - 2))
            left_pad = max(0, left_w - self._visible_len(left_clip))
            lines.append(f"  {left_clip}{' ' * left_pad}  {GRAY}|{R}  {right_clip}")

        lines.append(f"{GRAY}{'-' * w}{R}")
        lines.append(
            f"  {PINK_SOFT}naeha blend{R} {GRAY}|{R} curl http://localhost:{self.cfg['port']}/ {GRAY}|{R} curl http://localhost:{self.cfg['port']}{self.cfg['metrics_path']} {GRAY}|{R} curl http://localhost:{self.cfg['port']}/lb/status{R}"
        )
        return "\n".join(lines)

    def read_key(self) -> Optional[str]:
        if os.name == "nt":
            try:
                import msvcrt

                if msvcrt.kbhit():
                    ch = msvcrt.getch()
                    try:
                        return ch.decode("utf-8", errors="ignore")
                    except Exception:
                        return None
            except Exception:
                return None
            return None

        try:
            r, _, _ = select.select([sys.stdin], [], [], 0)
            if r:
                return sys.stdin.read(1)
        except Exception:
            return None
        return None

    def ui_loop(self) -> None:
        old_settings = None
        is_windows = os.name == "nt"
        if not is_windows:
            try:
                import termios  # type: ignore
                import tty  # type: ignore

                old_settings = termios.tcgetattr(sys.stdin)  # type: ignore
                tty.setcbreak(sys.stdin.fileno())  # type: ignore
            except Exception:
                old_settings = None

        try:
            if not is_windows:
                print("\033[?25l", end="")
            while not self.shutdown_event.is_set():
                key = self.read_key()
                if key == "1":
                    self.cfg["algorithm"] = "round_robin"
                    self.log("Algo -> Round Robin", "INFO")
                elif key == "2":
                    self.cfg["algorithm"] = "least_conn"
                    self.log("Algo -> Least Connections", "INFO")
                elif key == "3":
                    self.cfg["algorithm"] = "weighted"
                    self.log("Algo -> Weighted", "INFO")
                elif key == "4":
                    self.cfg["algorithm"] = "ip_hash"
                    self.log("Algo -> IP Hash", "INFO")
                elif key in ("m", "M"):
                    self.cycle_demo_mode()
                elif key == "7":
                    self.toggle_backend_forced(0)
                elif key == "8":
                    self.toggle_backend_forced(1)
                elif key == "9":
                    self.toggle_backend_forced(2)
                elif key in ("o", "O"):
                    with self.lock:
                        if self.synthetic_qps_target <= 0.01:
                            self.synthetic_qps_target = 40.0
                            self.log("overload burst -> ON (40 qps)", "WARN")
                        else:
                            self.synthetic_qps_target = 0.0
                            self.log("overload burst -> OFF", "OK")
                elif key == "q":
                    self.shutdown("ui")
                    break

                ui = self.render_ui()
                # \033[H moves cursor to Home, ui overwrites, \033[0J clears any trailing text. ZERO flicker!
                sys.stdout.write("\033[H" + ui + "\033[0J")
                sys.stdout.flush()
                time.sleep(max(0.05, float(self.cfg["ui_refresh_ms"]) / 1000.0))
        finally:
            if old_settings is not None:
                try:
                    import termios  # type: ignore

                    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)  # type: ignore
                except Exception:
                    pass
            print("\033[?25h\033[2J\033[H", end="")

    # ----------------------------- lifecycle -----------------------------
    def shutdown(self, source: str = "signal") -> None:
        if self.shutdown_event.is_set():
            return

        self.log(f"Shutdown initiated ({source})", "WARN")
        self.accepting = False
        self.shutdown_event.set()

        if self.listener:
            try:
                self.listener.close()
            except OSError:
                pass

        # drain in-flight work up to timeout
        deadline = time.time() + float(self.cfg["shutdown_timeout"])
        while time.time() < deadline:
            with self.lock:
                done = self.queue.empty() and self.in_flight == 0
            if done:
                break
            time.sleep(0.1)

        for srv in self.mock_servers.values():
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass

        self.log("Shutdown complete", "OK")

    def run(self) -> None:
        def _sig_handler(sig, frame):
            del sig, frame
            self.shutdown("signal")

        signal.signal(signal.SIGINT, _sig_handler)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _sig_handler)

        if bool(self.cfg.get("start_mock_backends", False)):
            self.start_mock_backends()
            time.sleep(0.3)

        self.apply_demo_mode("LIVE")

        synthetic_thread = threading.Thread(target=self.synthetic_load_loop, daemon=True, name="synthetic-loader")
        synthetic_thread.start()

        self.start_workers()

        health_thread = threading.Thread(target=self.health_loop, daemon=True, name="health")
        health_thread.start()

        listener_thread = threading.Thread(target=self.start_listener, daemon=True, name="listener")
        listener_thread.start()

        self.log("Ready. Send traffic with curl or browser", "OK")
        self.log("Controls: [1] RR [2] Least Conn [3] Weighted [q] Quit", "INFO")
        self.ui_loop()
        self.shutdown("ui-end")

    # ----------------------------- optional mock backends -----------------------------
    def start_mock_backends(self) -> None:
        responses = [
            b"I am server 1 (weight: 3)",
            b"I am server 2 (weight: 2)",
            b"I am server 3 (weight: 1)",
        ]

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                if self.path == "/health":
                    self.send_response(200)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"OK")
                    return

                time.sleep(random.uniform(0.005, 0.08))
                if random.random() < 0.05:
                    body = b"Internal Server Error"
                    self.send_response(500)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                idx = getattr(self.server, "server_index", 0)
                body = responses[idx] + f" | path: {self.path}".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                content_length = int(self.headers.get("Content-Length", "0"))
                payload = self.rfile.read(content_length) if content_length > 0 else b""
                body = b"echo: " + payload[:120]
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        for i, b in enumerate(self.cfg["backends"]):
            server = HTTPServer((str(b["host"]), int(b["port"])), Handler)
            setattr(server, "server_index", i)
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            self.mock_servers[str(b["id"])] = server
        ports = ", ".join(str(b["port"]) for b in self.cfg["backends"])
        self.log(f"Mock backends started on ports: {ports}", "INFO")


# ----------------------------- config and CLI -----------------------------
def parse_backend_spec(spec: str, default_id: int) -> Dict[str, Any]:
    """
    Backend format:
    - id@host:port:weight
    - host:port:weight
    - host:port
    """
    backend_id = f"srv-{default_id}"
    addr = spec
    if "@" in spec:
        backend_id, addr = spec.split("@", 1)

    parts = addr.split(":")
    if len(parts) < 2:
        raise ValueError(f"invalid backend spec: {spec}")

    host = parts[0]
    port = int(parts[1])
    weight = int(parts[2]) if len(parts) >= 3 else 1

    color_cycle = [GREEN, CYAN, MAGENTA, YELLOW, TEAL]
    color = color_cycle[(default_id - 1) % len(color_cycle)]
    return {"id": backend_id, "host": host, "port": port, "weight": weight, "color": color}


def build_config_from_args() -> Dict[str, Any]:
    parser = argparse.ArgumentParser(description="Raw-socket HTTP load balancer")
    parser.add_argument("--config", help="Path to JSON config file")

    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--algorithm", choices=["round_robin", "least_conn", "weighted"])
    parser.add_argument("--backend", action="append", help="Repeatable backend spec: id@host:port:weight")

    parser.add_argument("--max-workers", type=int)
    parser.add_argument("--queue-size", type=int)
    parser.add_argument("--health-interval", type=float)
    parser.add_argument("--health-timeout", type=float)
    parser.add_argument("--connect-timeout", type=float)
    parser.add_argument("--read-timeout", type=float)
    parser.add_argument("--max-header-kb", type=int)
    parser.add_argument("--shutdown-timeout", type=float)
    parser.add_argument("--retry-attempts", type=int)
    parser.add_argument("--backend-fail-threshold", type=int)
    parser.add_argument("--metrics-path")
    parser.add_argument("--ui-refresh-ms", type=int)

    parser.add_argument("--start-mock-backends", action="store_true", default=None)
    parser.add_argument("--no-mock-backends", action="store_true", default=None)

    # Benchmark mode
    parser.add_argument("--benchmark", action="store_true", help="Run a built-in benchmark")
    parser.add_argument("--duration", type=int, default=10, help="Benchmark duration in seconds")
    parser.add_argument("--concurrency", type=int, default=20, help="Benchmark concurrency")

    args = parser.parse_args()

    cfg = dict(DEFAULT_CONFIG)
    cfg["backends"] = [dict(b) for b in DEFAULT_CONFIG["backends"]]

    if args.config:
        with open(args.config, "r", encoding="utf-8") as f:
            file_cfg = json.load(f)
        cfg.update({k: v for k, v in file_cfg.items() if k != "backends"})
        if "backends" in file_cfg:
            cfg["backends"] = file_cfg["backends"]

    override_keys = [
        "host",
        "port",
        "algorithm",
        "max_workers",
        "queue_size",
        "health_interval",
        "health_timeout",
        "connect_timeout",
        "read_timeout",
        "max_header_kb",
        "shutdown_timeout",
        "retry_attempts",
        "backend_fail_threshold",
        "metrics_path",
        "ui_refresh_ms",
    ]

    for key in override_keys:
        arg_key = key.replace("-", "_")
        val = getattr(args, arg_key, None)
        if val is not None:
            cfg[key] = val

    if args.start_mock_backends:
        cfg["start_mock_backends"] = True
    if args.no_mock_backends:
        cfg["start_mock_backends"] = False

    if args.backend:
        parsed = []
        for idx, spec in enumerate(args.backend, start=1):
            parsed.append(parse_backend_spec(spec, idx))
        cfg["backends"] = parsed

    # normalize backend entries and provide defaults
    normalized = []
    for idx, b in enumerate(cfg["backends"], start=1):
        if isinstance(b, str):
            normalized.append(parse_backend_spec(b, idx))
            continue
        normalized.append(
            {
                "id": str(b.get("id", f"srv-{idx}")),
                "host": str(b.get("host", "127.0.0.1")),
                "port": int(b.get("port", 9000 + idx)),
                "weight": int(b.get("weight", 1)),
                "color": str(b.get("color", [GREEN, CYAN, MAGENTA, YELLOW, TEAL][(idx - 1) % 5])),
            }
        )
    cfg["backends"] = normalized
    
    cfg["benchmark"] = bool(getattr(args, "benchmark", False))
    cfg["benchmark_duration"] = int(getattr(args, "duration", 10))
    cfg["benchmark_concurrency"] = int(getattr(args, "concurrency", 20))

    return cfg


def main() -> None:
    if os.name == "nt":
        os.system("")  # Magic trick to enable native ANSI processing on Windows CMD
    print("\033[2J\033[H", end="")
    print(f"\n{PINK_BRIGHT}{BOLD}Starting load balancer...{R}")
    for idx, line in enumerate(STARTUP_BANNER):
        color = PINK_BRIGHT if idx in (0, 5) else PINK_SOFT
        print(f"{color}{line}{R}")
    print()

    cfg = build_config_from_args()
    
    if cfg.get("benchmark"):
        def run_benchmark():
            print(f"\n{PINK_BRIGHT}{BOLD}*** STARTING BENCHMARK MODE ***{R}")
            print(f"Target: http://{cfg['host']}:{cfg['port']}/")
            print(f"Concurrency: {cfg['benchmark_concurrency']} threads")
            print(f"Duration: {cfg['benchmark_duration']} seconds\n")
            
            url = f"http://{cfg['host']}:{cfg['port']}/"
            stats = {"reqs": 0, "errors": 0, "latencies": []}
            running = True
            
            def worker():
                while running:
                    start = time.time()
                    try:
                        req = urllib.request.Request(url, method="GET")
                        with urllib.request.urlopen(req, timeout=2.0) as resp:
                            resp.read()
                        stats["reqs"] += 1
                        stats["latencies"].append(time.time() - start)
                    except Exception:
                        stats["errors"] += 1
            
            threads = []
            for _ in range(int(cfg["benchmark_concurrency"])):
                t = threading.Thread(target=worker, daemon=True)
                t.start()
                threads.append(t)
                
            time.sleep(float(cfg["benchmark_duration"]))
            running = False
            
            print(f"\n{PINK_BRIGHT}{BOLD}--- BENCHMARK RESULTS ---{R}")
            print(f"Total Requests: {stats['reqs']}")
            print(f"Errors:         {stats['errors']}")
            if stats['latencies']:
                avg = sum(stats['latencies']) / len(stats['latencies']) * 1000
                p95 = sorted(stats['latencies'])[int(len(stats['latencies'])*0.95)] * 1000
                print(f"Avg Latency:    {avg:.2f} ms")
                print(f"p95 Latency:    {p95:.2f} ms")
            print(f"Throughput:     {stats['reqs']/float(cfg['benchmark_duration']):.2f} req/s\n")
            
            # Delay slightly and exit process exactly once benchmark finishes
            time.sleep(0.5)
            os._exit(0)
            
        t = threading.Timer(1.5, run_benchmark)
        t.daemon = True
        t.start()

    lb = LoadBalancer(cfg)
    lb.run()


if __name__ == "__main__":
    main()
