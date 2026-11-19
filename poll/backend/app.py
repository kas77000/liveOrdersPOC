"""FastAPI server, polling version: every POLL_SECONDS reads the new RDB rows, diffs the
blotter, and pushes the changed rows to the browsers over a WebSocket.

    KDB_HOST / KDB_PORT / KDB_USER / KDB_PASS    OMS RDB endpoint (unset -> simulated data)
    POLL_SECONDS                                 poll period (default 1.0)
    VPROF_HOST / VPROF_PORT                      VPROF gateway, for the volume profile
                                                 modal (unset -> simulated profile)

Run:  run.bat   then open http://localhost:8000
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

from .sources import KdbSource, MockSource
from .profile import (SYM_RE, MockVprof, ProfileError, VprofSource, order_profile,
                      region_now_minute, tilt_full_day, tilt_order_profile)
from .today import MockQatt, QattSource, today_vs_profile
from .transform import build_rows

log = logging.getLogger("liveorders")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
MODE = "poll"
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "1.0"))


def make_source():
    host = os.getenv("KDB_HOST")
    if host:
        return KdbSource(host, int(os.getenv("KDB_PORT", "5000")),
                         os.getenv("KDB_USER", ""), os.getenv("KDB_PASS", ""))
    return MockSource()


def make_qatt_source():
    """qatt on the QUOTES real-time server (today's prints); simulated without it."""
    host = os.getenv("QATT_HOST")
    if host:
        return QattSource(host, int(os.getenv("QATT_PORT", "5000")),
                          os.getenv("QATT_USER", os.getenv("KDB_USER", "")),
                          os.getenv("QATT_PASS", os.getenv("KDB_PASS", "")))
    return MockQatt(os.getenv("REGION_TZ", "Asia/Hong_Kong"))


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
            t0 = time.monotonic()
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
            await asyncio.to_thread(self.source.wait, POLL_SECONDS - (time.monotonic() - t0))

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
qatt = make_qatt_source()
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
    qatt.close()


app = FastAPI(title="Live Orders POC", lifespan=lifespan)


@app.get("/api/orders")
def orders():
    return hub.snapshot()


@app.get("/api/profile")
def volume_profile(sym: str, alpha: float | None = None, beta: float | None = None, mode: int = 0):
    """The symbol's whole-day volume profile, auctions included (see profile.py).
    alpha / beta / mode: the tag-148 tilt, as if an order ran from the open to the close.
    Plain def: FastAPI runs it in a worker thread, so a slow gateway never blocks
    the blotter."""
    if not SYM_RE.fullmatch(sym or ""):
        return JSONResponse({"ok": False, "sym": sym, "error": "not a symbol"}, status_code=400)
    try:
        full = vprof.get(sym)
        if alpha is not None and beta is not None:
            full = tilt_full_day(full, alpha, beta, mode)
        return {"ok": True, "source": vprof.label, **full}
    except Exception as e:
        log.warning("profile for %s failed: %s", sym, e)
        return JSONResponse({"ok": False, "sym": sym, "source": vprof.label,
                             "error": f"{type(e).__name__}: {e}"}, status_code=502)


@app.get("/api/today")
def today_volume(sym: str, now: float | None = None):
    """Today's traded volume per profile bucket (qatt) and the forecast to the close
    (see today.py). `now` (minute of the day, region clock) overrides the clock, to
    look at a moment of the day."""
    if not SYM_RE.fullmatch(sym or ""):
        return JSONResponse({"ok": False, "sym": sym, "error": "not a symbol"}, status_code=400)
    try:
        full = vprof.get(sym)
    except Exception as e:
        return JSONResponse({"ok": False, "sym": sym, "source": vprof.label,
                             "error": f"profile: {type(e).__name__}: {e}"}, status_code=502)
    t = region_now_minute(REGION_TZ) if now is None else now
    try:
        minutes = qatt.get(sym, full, now=t)
    except Exception as e:
        log.warning("qatt for %s failed: %s", sym, e)
        return JSONResponse({"ok": False, "sym": sym, "source": qatt.label,
                             "error": f"{type(e).__name__}: {e}"}, status_code=502)
    return {"ok": True, "source": qatt.label, **today_vs_profile(full, minutes, t)}


@app.get("/api/order_profile")
def order_volume_profile(sym: str, start: int, end: int, open: int = 1, close: int = 1,
                         alpha: float | None = None, beta: float | None = None, mode: int = 0):
    """The full-day profile cut to an order's window [start, end) (minutes, region
    clock) and rescaled to 100% of the order. Reuses the cached full-day profile.
    alpha / beta / mode: the tag-148 tilt, replayed as ai3's ProfileSnapshot does."""
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
        if alpha is not None and beta is not None:
            out = tilt_order_profile(out, alpha, beta, mode)
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
