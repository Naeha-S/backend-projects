"""High-performance data plane with packet batching and buffered I/O."""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger("naeha.data_plane")


@dataclass
class PacketBatch:
    """A batch of packets to process together."""
    packets: list[bytes]
    timestamps: list[float]
    
    def __len__(self) -> int:
        return len(self.packets)
    
    def clear(self) -> None:
        self.packets.clear()
        self.timestamps.clear()


class BufferedWriter:
    """Buffers writes and flushes in batches to reduce syscall overhead."""
    
    def __init__(self, sock: socket.socket, batch_size: int = 8, flush_interval: float = 0.001):
        self.sock = sock
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.buffer: list[tuple[bytes, tuple[str, int]]] = []
        self.last_flush = time.time()
        self.lock = threading.Lock()
        self.bytes_pending = 0
    
    def write(self, data: bytes, address: tuple[str, int]) -> None:
        """Add a packet to the write buffer."""
        with self.lock:
            self.buffer.append((data, address))
            self.bytes_pending += len(data)
            
            # Flush if batch is full or time to flush
            should_flush = (
                len(self.buffer) >= self.batch_size or
                (time.time() - self.last_flush) > self.flush_interval
            )
            
            if should_flush:
                self._flush()
    
    def _flush(self) -> None:
        """Send all buffered packets."""
        if not self.buffer:
            return
        
        flushed = 0
        for data, address in self.buffer:
            try:
                self.sock.sendto(data, address)
                flushed += 1
            except OSError as e:
                log.warning(f"Socket write failed after {flushed} packets: {e}")
                break
        
        self.buffer.clear()
        self.bytes_pending = 0
        self.last_flush = time.time()
        log.debug(f"Flushed {flushed} packets")
    
    def flush(self) -> None:
        """Force flush of all pending packets."""
        with self.lock:
            self._flush()


class TunBatchReader:
    """Reads packets from TUN device in batches for better throughput."""
    
    def __init__(self, tun_device, batch_size: int = 16, read_timeout: float = 0.01):
        self.tun = tun_device
        self.batch_size = batch_size
        self.read_timeout = read_timeout
    
    def read_batch(self) -> PacketBatch:
        """Read up to batch_size packets from TUN device."""
        batch = PacketBatch([], [])
        start = time.time()
        
        while len(batch) < self.batch_size:
            # Allow timeout to prevent blocking forever
            if (time.time() - start) > self.read_timeout:
                break
            
            try:
                # Set non-blocking if possible, otherwise use timeout
                packet = self.tun.read(65535)
                if packet:
                    batch.packets.append(packet)
                    batch.timestamps.append(time.time())
            except OSError:
                break
        
        return batch


class DataPlaneMetrics:
    """Tracks data plane performance metrics."""
    
    def __init__(self):
        self.lock = threading.Lock()
        self.tun_packets_in = 0
        self.tun_packets_out = 0
        self.tun_bytes_in = 0
        self.tun_bytes_out = 0
        self.encrypted_packets_sent = 0
        self.encrypted_bytes_sent = 0
        self.decrypted_packets_received = 0
        self.decrypted_bytes_received = 0
        self.socket_sends = 0
        self.socket_send_errors = 0
        self.socket_recv_errors = 0
        self.batches_processed = 0
        self.avg_batch_size = 0.0
        # Required counters
        self.socket_packets_received = 0
        self.packets_decrypted_success = 0
        self.packets_decrypted_failed = 0
        self.packets_written_to_tun = 0
    
    def record_socket_recv(self) -> None:
        with self.lock:
            self.socket_packets_received += 1
            
    def record_decrypt_success(self) -> None:
        with self.lock:
            self.packets_decrypted_success += 1
            
    def record_decrypt_failed(self) -> None:
        with self.lock:
            self.packets_decrypted_failed += 1
            
    def record_tun_write(self) -> None:
        with self.lock:
            self.packets_written_to_tun += 1
    
    def record_tun_in(self, packet_count: int, byte_count: int) -> None:
        with self.lock:
            self.tun_packets_in += packet_count
            self.tun_bytes_in += byte_count
    
    def record_tun_out(self, packet_count: int, byte_count: int) -> None:
        with self.lock:
            self.tun_packets_out += packet_count
            self.tun_bytes_out += byte_count
    
    def record_encrypt(self, packet_count: int, byte_count: int) -> None:
        with self.lock:
            self.encrypted_packets_sent += packet_count
            self.encrypted_bytes_sent += byte_count
    
    def record_decrypt(self, packet_count: int, byte_count: int) -> None:
        with self.lock:
            self.decrypted_packets_received += packet_count
            self.decrypted_bytes_received += byte_count
    
    def record_batch(self, batch_size: int) -> None:
        with self.lock:
            self.batches_processed += 1
            # Rolling average
            self.avg_batch_size = (
                (self.avg_batch_size * (self.batches_processed - 1) + batch_size) / 
                self.batches_processed
            )
    
    def record_socket_send(self, count: int = 1) -> None:
        with self.lock:
            self.socket_sends += count
    
    def record_socket_error(self, is_send: bool = True) -> None:
        with self.lock:
            if is_send:
                self.socket_send_errors += 1
            else:
                self.socket_recv_errors += 1
    
    def snapshot(self) -> dict:
        """Return current metrics snapshot."""
        with self.lock:
            return {
                "tun_packets_in": self.tun_packets_in,
                "tun_packets_out": self.tun_packets_out,
                "tun_bytes_in": self.tun_bytes_in,
                "tun_bytes_out": self.tun_bytes_out,
                "encrypted_packets_sent": self.encrypted_packets_sent,
                "encrypted_bytes_sent": self.encrypted_bytes_sent,
                "decrypted_packets_received": self.decrypted_packets_received,
                "decrypted_bytes_received": self.decrypted_bytes_received,
                "socket_sends": self.socket_sends,
                "socket_send_errors": self.socket_send_errors,
                "socket_recv_errors": self.socket_recv_errors,
                "batches_processed": self.batches_processed,
                "avg_batch_size": round(self.avg_batch_size, 2),
                "socket_packets_received": self.socket_packets_received,
                "packets_decrypted_success": self.packets_decrypted_success,
                "packets_decrypted_failed": self.packets_decrypted_failed,
                "packets_written_to_tun": self.packets_written_to_tun,
            }
