"""Buffered UDP transport with a queued sender."""

from __future__ import annotations

import queue
import socket
import threading


class UdpTransport:
    def __init__(self, host: str, port: int, *, recv_size: int = 65535):
        self.recv_size = recv_size
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        self.sock.bind((host, port))
        self.sock.settimeout(0.5)
        self._sendq: queue.Queue[tuple[bytes, tuple[str, int]]] = queue.Queue(maxsize=8192)
        self._running = threading.Event()
        self._sender: threading.Thread | None = None

    def start(self) -> None:
        self._running.set()
        self._sender = threading.Thread(target=self._send_loop, daemon=True, name="udp-send")
        self._sender.start()

    def enqueue(self, packet: bytes, address: tuple[str, int]) -> bool:
        try:
            self._sendq.put_nowait((packet, address))
            return True
        except queue.Full:
            return False

    def recv(self) -> tuple[bytes, tuple[str, int]]:
        return self.sock.recvfrom(self.recv_size)

    def close(self) -> None:
        self._running.clear()
        try:
            self.sock.close()
        except OSError:
            pass

    def _send_loop(self) -> None:
        while self._running.is_set():
            try:
                first = self._sendq.get(timeout=0.5)
            except queue.Empty:
                continue
            batch = [first]
            while len(batch) < 32:
                try:
                    batch.append(self._sendq.get_nowait())
                except queue.Empty:
                    break
            for packet, address in batch:
                try:
                    self.sock.sendto(packet, address)
                except OSError:
                    if not self._running.is_set():
                        return
