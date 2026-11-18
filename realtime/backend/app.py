"""FastAPI server, real-time version: woken by tickerplant updates (batched per 100 ms),
diffs the blotter, and pushes the changed rows to the browsers over a WebSocket.

    KDB_HOST / KDB_PORT / KDB_USER / KDB_PASS    OMS RDB, for the starting snapshot
    TP_HOST / TP_PORT                            tickerplant (either host unset -> simulated data)
    SWEEP_SECONDS                                RDB catch-up read period (default 10)
    VPROF_HOST / VPROF_PORT                      VPROF gateway, for the volume profile
                                                 modal (unset -> simulated profile)

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
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .sources import KdbSource, MockStreamSource, RealtimeSource
from .profile import SYM_RE, MockVprof, ProfileError, VprofSource, order_profile, region_now_minute
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


def make_profile_source():
    host = os.getenv("VPROF_HOST")
    if host:
        return VprofSource(host, int(os.getenv("VPROF_PORT", "5000")),
                           os.getenv("VPROF_USER", os.getenv("KDB_USER", "")),
                           os.getenv("VPROF_PASS", os.getenv("KDB_PASS", "")))
    return MockVprof()


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
vprof = make_profile_source()
# the clock the profile, the orders and qatt share; "now" for the fill marker
REGION_TZ = os.getenv("REGION_TZ", "Asia/Hong_Kong")


@asynccontextmanager
async def lifespan(_app):
    log.info("mode: %s, source: %s", MODE, hub.source.label)
    task = asyncio.create_task(hub.poll_forever())
    yield
    task.cancel()
    hub.source.close()
    vprof.close()


app = FastAPI(title="Live Orders POC", lifespan=lifespan)


@app.get("/api/orders")
def orders():
    return hub.snapshot()


@app.get("/api/profile")
def volume_profile(sym: str):
    """The symbol's whole-day volume profile, auctions included (see profile.py).
    Plain def: FastAPI runs it in a worker thread, so a slow gateway never blocks
    the blotter."""
    if not SYM_RE.fullmatch(sym or ""):
        return JSONResponse({"ok": False, "sym": sym, "error": "not a symbol"}, status_code=400)
    try:
        return {"ok": True, "source": vprof.label, **vprof.get(sym)}
    except Exception as e:
        log.warning("profile for %s failed: %s", sym, e)
        return JSONResponse({"ok": False, "sym": sym, "source": vprof.label,
                             "error": f"{type(e).__name__}: {e}"}, status_code=502)


@app.get("/api/order_profile")
def order_volume_profile(sym: str, start: int, end: int, open: int = 1, close: int = 1):
    """The full-day profile cut to an order's window [start, end) (minutes, region
    clock) and rescaled to 100% of the order. Reuses the cached full-day profile."""
    if not SYM_RE.fullmatch(sym or ""):
        return JSONResponse({"ok": False, "sym": sym, "error": "not a symbol"}, status_code=400)
    try:
        full = vprof.get(sym)
    except Exception as e:
        log.warning("profile for %s failed: %s", sym, e)
        return JSONResponse({"ok": False, "sym": sym, "source": vprof.label,
                             "error": f"{type(e).__name__}: {e}"}, status_code=502)
    try:
        out = order_profile(full, start, end, bool(open), bool(close))
    except ProfileError as e:
        return JSONResponse({"ok": False, "sym": sym, "source": vprof.label, "error": str(e)},
                            status_code=422)
    return {"ok": True, "source": vprof.label, "now_min": region_now_minute(REGION_TZ), **out}


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
