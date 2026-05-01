"""FastAPI monitoring backend for NaehaVPN."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from starlette.websockets import WebSocketDisconnect
import uvicorn

from .metrics import MetricsRegistry


class DashboardServer:
    def __init__(self, metrics: MetricsRegistry, host: str, port: int, interval_seconds: float = 1.0):
        self.metrics = metrics
        self.host = host
        self.port = port
        self.interval_seconds = interval_seconds
        self.app = FastAPI(title="NaehaVPN dashboard", version="1.0")
        self._mount_routes()

    def _mount_routes(self) -> None:
        @self.app.get("/")
        async def root() -> FileResponse:
            dashboard_path = Path(__file__).parent.parent / "dashboard.html"
            return FileResponse(dashboard_path, media_type="text/html")

        @self.app.get("/health")
        async def health() -> JSONResponse:
            return JSONResponse({"status": "ok", "product": "NaehaVPN"})

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
                pass
            except Exception:
                pass
            finally:
                try:
                    await websocket.close()
                except Exception:
                    pass

    def start(self) -> None:
        config = uvicorn.Config(self.app, host=self.host, port=self.port, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True, name="dashboard")
        thread.start()
