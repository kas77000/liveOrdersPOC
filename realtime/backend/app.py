"""FastAPI server, real-time version: woken by tickerplant updates (batched per 100 ms),
diffs the blotter, and pushes the changed rows to the browsers over a WebSocket.

    KDB_HOST / KDB_PORT / KDB_USER / KDB_PASS    OMS RDB, for the starting snapshot
    TP_HOST / TP_PORT                            tickerplant (either host unset -> simulated data)
    SWEEP_SECONDS                                RDB catch-up read period (default 10)

Run:  run.bat   then open http://localhost:8001
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from .sources import KdbSource, MockStreamSource, RealtimeSource
from .transform import build_rows

log = logging.getLogger("liveorders")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
MODE = "realtime"
# wake at least this often even with no ticks, to refresh the status line
IDLE_SECONDS = 5.0


def make_source():
    host, tp_host = os.getenv("KDB_HOST"), os.getenv("TP_HOST")
    if not (host and tp_host):
        return MockStreamSource()
    rdb = KdbSource(host, int(os.getenv("KDB_PORT", "5000")),
                    os.getenv("KDB_USER", ""), os.getenv("KDB_PASS", ""))
    return RealtimeSource(tp_host, int(os.getenv("TP_PORT", "5010")), rdb,
                          float(os.getenv("SWEEP_SECONDS", "10")))


class Hub:
    """Holds the current blotter and fans deltas out to every connected browser."""

    def __init__(self, source):
        self.source = source
        self.rows: dict[str, dict] = {}
        self.clients: set[WebSocket] = set()
        self.status = {"source": source.label, "mode": MODE, "ok": False, "error": None,
                       "updated": None}

    def snapshot(self) -> dict:
        return {"type": "snapshot", "rows": list(self.rows.values()), "status": self.status}

    async def poll_forever(self):
        while True:
            try:
                frames = await asyncio.to_thread(self.source.fetch)
                new = {r["key"]: r for r in await asyncio.to_thread(lambda: build_rows(**frames))}
                upserts = [r for k, r in new.items() if self.rows.get(k) != r]
                removes = [k for k in self.rows if k not in new]
                self.rows = new
                was_ok = self.status["ok"]
                self.status.update(ok=True, error=None, updated=time.time())
                if upserts or removes or not was_ok:
                    await self.broadcast({"type": "delta", "upserts": upserts,
                                          "removes": removes, "status": self.status})
            except Exception as e:  # keep serving the last good blotter
                log.warning("poll failed: %s", e)
                self.status.update(ok=False, error=f"{type(e).__name__}: {e}")
                await self.broadcast({"type": "status", "status": self.status})
            await asyncio.to_thread(self.source.wait, IDLE_SECONDS)

    async def broadcast(self, msg: dict):
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


hub = Hub(make_source())


@asynccontextmanager
async def lifespan(_app):
    log.info("mode: %s, source: %s", MODE, hub.source.label)
    task = asyncio.create_task(hub.poll_forever())
    yield
    task.cancel()
    hub.source.close()


app = FastAPI(title="Live Orders POC", lifespan=lifespan)


@app.get("/api/orders")
def orders():
    return hub.snapshot()


@app.websocket("/ws")
async def ws_orders(ws: WebSocket):
    await ws.accept()
    hub.clients.add(ws)
    try:
        await ws.send_json(hub.snapshot())
        while True:
            await ws.receive_text()   # keep-alive; client sends nothing meaningful
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.discard(ws)


app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
