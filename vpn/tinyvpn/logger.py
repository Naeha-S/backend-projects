"""Structured logging for NaehaVPN with professional output."""

from __future__ import annotations

import logging
import sys
from datetime import datetime


class NaehaFormatter(logging.Formatter):
    """Professional formatter for NaehaVPN logs."""
    
    LEVEL_COLORS = {
        'DEBUG': '\033[36m',    # Cyan
        'INFO': '\033[32m',     # Green
        'WARNING': '\033[33m',  # Yellow
        'ERROR': '\033[31m',    # Red
        'CRITICAL': '\033[35m', # Magenta
    }
    RESET = '\033[0m'
    
    def format(self, record: logging.LogRecord) -> str:
        """Format log record with colors and structure."""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        level = record.levelname
        
        # Color the level
        color = self.LEVEL_COLORS.get(level, '')
        colored_level = f"{color}{level}{self.RESET}"
        
        # Module name
        module = record.name.replace('naeha.', '')
        
        # Build message
        message = record.getMessage()
        
        # Format
        if record.exc_info:
            message += f"\n{self.formatException(record.exc_info)}"
        
        return f"{timestamp} {colored_level:20} [{module:20}] {message}"


def setup_logging(name: str = "naeha", level: int = logging.INFO, debug: bool = False) -> logging.Logger:
    """Set up structured logging for NaehaVPN."""
    if debug:
        level = logging.DEBUG
    
    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    # Remove existing handlers
    logger.handlers.clear()
    
    # Console handler with formatter
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    formatter = NaehaFormatter()
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    
    # Suppress noise from other libraries
    logging.getLogger('uvicorn').setLevel(logging.WARNING)
    logging.getLogger('uvicorn.access').setLevel(logging.WARNING)
    logging.getLogger('asyncio').setLevel(logging.WARNING)
    
    return logger


class PacketFlowLogger:
    """Debug logging for packet lifecycle (optional, performance cost)."""
    
    def __init__(self, enabled: bool = False):
        self.enabled = enabled
        self.log = logging.getLogger("naeha.packet_flow")
        self.log.setLevel(logging.DEBUG if enabled else logging.CRITICAL)
    
    def tun_read(self, packet_id: str, size: int) -> None:
        if self.enabled:
            self.log.debug(f"TUN_READ {packet_id} {size}B")
    
    def encrypt(self, packet_id: str, size_in: int, size_out: int) -> None:
        if self.enabled:
            self.log.debug(f"ENCRYPT {packet_id} {size_in}B -> {size_out}B")
    
    def socket_send(self, packet_id: str, address: str, size: int) -> None:
        if self.enabled:
            self.log.debug(f"SOCKET_SEND {packet_id} to {address} {size}B")
    
    def socket_recv(self, packet_id: str, address: str, size: int) -> None:
        if self.enabled:
            self.log.debug(f"SOCKET_RECV {packet_id} from {address} {size}B")
    
    def decrypt(self, packet_id: str, size_in: int, size_out: int, success: bool) -> None:
        if self.enabled:
            status = "OK" if success else "FAIL"
            self.log.debug(f"DECRYPT {packet_id} {size_in}B -> {size_out}B [{status}]")
    
    def tun_write(self, packet_id: str, size: int) -> None:
        if self.enabled:
            self.log.debug(f"TUN_WRITE {packet_id} {size}B")
