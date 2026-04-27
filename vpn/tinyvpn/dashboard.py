"""FastAPI monitoring backend."""

from __future__ import annotations

import asyncio
import threading

from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse
import uvicorn

from .metrics import MetricsRegistry

from starlette.websockets import WebSocketDisconnect


class DashboardServer:
    def __init__(self, metrics: MetricsRegistry, host: str, port: int, interval_seconds: float = 1.0):
        self.metrics = metrics
        self.host = host
        self.port = port
        self.interval_seconds = interval_seconds
        self.app = FastAPI(title="tinyvpn dashboard", version="3.0")
        self._mount_routes()

    def _mount_routes(self) -> None:
        @self.app.get("/health")
        async def health() -> JSONResponse:
            return JSONResponse({"status": "ok"})

        @self.app.get("/api/snapshot")
        async def snapshot() -> JSONResponse:
            return JSONResponse(self.metrics.snapshot())

        @self.app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            try:
                while True:
                    await websocket.send_json(self.metrics.snapshot())
                    await asyncio.sleep(self.interval_seconds)
            except WebSocketDisconnect:
                pass  # Client disconnected gracefully
            except Exception:
                pass  # Ignore other connection-related errors on exit
            finally:
                try:
                    await websocket.close()
                except:
                    pass

    def start(self) -> None:
        config = uvicorn.Config(self.app, host=self.host, port=self.port, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True, name="dashboard")
        thread.start()
