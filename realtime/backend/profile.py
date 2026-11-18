"""Volume profile for one symbol: the whole day, auctions included.

Ported from experiment6/core-vp (core/profile.py, core/session.py), which follows
ai3's getsymprof (getVolumeProfileReport.q). The VPROF gateway returns `vst` rows:

    time=0    adv       median daily CONTINUOUS volume (shares)
    time=1    vopen     open auction volume
    time=2    vclose    close auction volume
    time=3    vopenpm   PM reopen auction volume (lunch-break markets)
    time>=100           minute of day (region clock); vmed is the CUMULATIVE
                        fraction of continuous volume, 0 at the open -> 1 at close

The auctions are not in the curve, so the final profile is built as getsymprof does:
interpolate the curve onto a 10-minute grid, scale the buckets by adv / vtot, and
put OPEN / PM / CLOSE around them at auction / vtot. vtot includes the PM auction
(getsymprof omits it and lunch-break days total slightly over 1).

    VprofSource   the gateway: get_data_by_date[`profile;...] for today
    MockVprof     a plausible profile, for running without kdb
    final_profile the transformation; pure, unit-tested
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import statistics
import threading
import zlib

import numpy as np
import pandas as pd

log = logging.getLogger("liveorders")

MIN_BUCKET_MINUTE = 100
CUM_TOLERANCE = 0.01
GRID_STEP = 10          # minutes; getsymprof's ftg grid (600000 ms)
LUNCH_GAP_FACTOR = 2.0
ROW_ADV, ROW_OPEN, ROW_CLOSE, ROW_OPEN_PM = 0, 1, 2, 3
SYM_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,39}")


class ProfileError(ValueError):
    """The rows cannot be decoded into a usable profile."""


def clock(minute) -> str:
    m = int(minute) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


# --------------------------------------------------------------------------- decode
def _header_skip(raw: pd.DataFrame) -> int:
    """Where the buckets start: cc0 on row 0 when the gateway returns it, else the
    first row whose time looks like a minute of day."""
    if "cc0" in raw.columns and len(raw):
        try:
            n = int(raw["cc0"].iloc[0])
            if 0 < n < len(raw):
                return n
        except (TypeError, ValueError):
            pass
    mask = (raw["time"] >= MIN_BUCKET_MINUTE).to_numpy()
    if not mask.any():
        raise ProfileError(f"no bucket rows: every `time` is below {MIN_BUCKET_MINUTE}")
    return int(np.argmax(mask))


def _lunch(minutes: list[int]) -> tuple[int, int] | None:
    """The lunch break is the widest gap between bucket minutes when it exceeds twice
    the median spacing (Shenzhen 690 -> 780, Tokyo 630 -> 690)."""
    gaps = [b - a for a, b in zip(minutes, minutes[1:])]
    if not gaps:
        return None
    median, widest = statistics.median(gaps), max(gaps)
    if median > 0 and widest > LUNCH_GAP_FACTOR * median:
        i = gaps.index(widest)
        return minutes[i], minutes[i + 1]
    return None


def decode(raw: pd.DataFrame) -> dict:
    """Header volumes, the continuous cumulative curve and the session from vst rows."""
    if raw is None or len(raw) == 0:
        raise ProfileError("no profile rows returned")
    raw = raw.sort_values("time").reset_index(drop=True)

    head = raw[raw["time"] < MIN_BUCKET_MINUTE].drop_duplicates(subset="time")
    head = head.set_index("time")["vmed"]
    missing = [r for r in (ROW_ADV, ROW_OPEN, ROW_CLOSE) if r not in head.index]
    if missing:
        raise ProfileError(f"missing header row(s) {missing}: expected time=0/1/2 "
                           "for adv / open auction / close auction")

    body = raw.iloc[_header_skip(raw):]
    body = body[body["time"] >= MIN_BUCKET_MINUTE]
    # Keep the last pre-open zero row: it is the session open, where the curve is 0.
    positive = np.flatnonzero(body["vmed"].to_numpy() > 0.0)
    if len(positive) == 0:
        raise ProfileError("every bucket is zero")
    body = body.iloc[max(0, int(positive[0]) - 1):]
    if len(body) < 2:
        raise ProfileError("fewer than 2 buckets; cannot form a curve")

    minutes = body["time"].astype("int64").tolist()
    cum = body["vmed"].astype(float).to_numpy()
    if np.any(np.diff(cum) < -1e-12):
        raise ProfileError("vmed is not non-decreasing, so it is not a cumulative curve")
    if abs(cum[-1] - 1.0) > CUM_TOLERANCE:
        raise ProfileError(f"vmed does not reach 1.0 (ends at {cum[-1]:.4f})")

    p = dict(adv=float(head.get(ROW_ADV, 0.0)), v_open=float(head.get(ROW_OPEN, 0.0)),
             v_close=float(head.get(ROW_CLOSE, 0.0)),
             v_open_pm=float(head.get(ROW_OPEN_PM, 0.0)),
             minutes=minutes, cum=cum.tolist(),
             open_min=minutes[0], close_min=minutes[-1], lunch=_lunch(minutes))
    p["vtot"] = p["adv"] + p["v_open"] + p["v_close"] + p["v_open_pm"]
    if p["vtot"] <= 0:
        raise ProfileError(f"total volume is {p['vtot']}")
    return p


# --------------------------------------------------------------------------- final
def _grid(start: int, end: int, step: int) -> list[int]:
    """ftg: every `step` minutes from `start`, ending exactly on `end` (a short last
    bucket rather than dropping the tail, so the day sums to 1)."""
    return list(range(int(start), int(end), int(step))) + [int(end)]


def final_profile(p: dict, step: int = GRID_STEP) -> list[dict]:
    """OPEN, 10-minute continuous buckets (PM at the lunch reopen), CLOSE.

    Each row: start/end (minutes, region clock; a bar covers [start, end)), label,
    phase, share (of the whole day's volume) and cum (running total, ends at 1).
    """
    x, y = np.asarray(p["minutes"], float), np.asarray(p["cum"], float)
    vtot, scale = p["vtot"], p["adv"] / p["vtot"]
    o, c, lunch = p["open_min"], p["close_min"], p["lunch"]

    rows = [dict(start=o - step, end=o, label="OPEN", phase="open auction",
                 share=p["v_open"] / vtot)]
    segments = [(o, c)] if lunch is None else [(o, lunch[0]), (lunch[1], c)]
    prev = float(np.interp(o, x, y))
    for k, (a, b) in enumerate(segments):
        if k == 1 and p["v_open_pm"] > 0:
            rows.append(dict(start=a - step, end=a, label="PM", phase="pm auction",
                             share=p["v_open_pm"] / vtot))
        g = _grid(a, b, step)
        # Bucket i covers [g[i], g[i+1]). The first afternoon bucket is measured from
        # the last morning point, so volume the rows put inside lunch is not dropped.
        for a_i, b_i, cum in zip(g[:-1], g[1:], np.interp(g, x, y)[1:]):
            rows.append(dict(start=a_i, end=b_i, label=clock(a_i), phase="continuous",
                             share=max(float(cum) - prev, 0.0) * scale))
            prev = float(cum)
    rows.append(dict(start=c, end=c + step, label="CLOSE", phase="close auction",
                     share=p["v_close"] / vtot))

    total = 0.0
    for r in rows:
        total += r["share"]
        r["cum"] = total
    return rows


def build(raw: pd.DataFrame, sym: str, date: dt.date) -> dict:
    """What /api/profile returns."""
    p = decode(raw)
    return dict(sym=sym, date=date.isoformat(), adv=p["adv"], v_open=p["v_open"],
                v_close=p["v_close"], v_open_pm=p["v_open_pm"], vtot=p["vtot"],
                open_min=p["open_min"], close_min=p["close_min"], lunch=p["lunch"],
                rows=final_profile(p))


# --------------------------------------------------------------------------- sources
def profile_query(sym: str, date: dt.date, with_cc0: bool = True) -> str:
    """The gateway call as one string (the gateway refuses pykx's argument form).
    `profile` is the gateway's dataset alias, not a table name."""
    if not SYM_RE.fullmatch(sym or ""):
        # sym is spliced into q source: anything but a ticker is refused outright
        raise ProfileError(f"not a symbol: {sym!r}")
    cols = "`date`sym`vmed`time" + ("`cc0" if with_cc0 else "")
    day = f"{date:%Y.%m.%d}"
    return f"get_data_by_date[`profile;{cols};{day};{day};`{sym}]"


class VprofSource:
    """The VPROF gateway. Today's profile, falling back up to LOOKBACK business days
    when today's is not there yet. One handle, serialised, reopened on failure;
    results cached per symbol and day."""

    LOOKBACK = 5

    def __init__(self, host: str, port: int, user: str = "", password: str = ""):
        self.host, self.port, self.user, self.password = host, port, user, password
        self._q = None
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, dt.date], dict] = {}

    label = property(lambda self: f"VPROF {self.host}:{self.port}")

    def _handle(self):
        if self._q is None:
            import pykx as kx
            # no_ctx: the gateway defines its own `q`, which pykx's context
            # interface trips over (as found in core-vp).
            self._q = kx.SyncQConnection(host=self.host, port=self.port,
                                         username=self.user, password=self.password,
                                         timeout=15.0, no_ctx=True)
        return self._q

    def _rows(self, sym: str, date: dt.date) -> pd.DataFrame:
        try:
            res = self._handle()(profile_query(sym, date, with_cc0=True))
        except Exception as e:
            if "cc0" not in str(e):      # only retry when the column is the complaint
                raise
            res = self._handle()(profile_query(sym, date, with_cc0=False))
        try:
            return res.pd()
        except Exception:
            val = res.py()
            if isinstance(val, (bytes, str)):    # a restricted gateway answers in text
                raise RuntimeError(f"gateway: {val.decode() if isinstance(val, bytes) else val}")
            return pd.DataFrame(val)

    def get(self, sym: str, today: dt.date | None = None) -> dict:
        today = today or dt.date.today()
        if (sym, today) in self._cache:
            return self._cache[(sym, today)]
        with self._lock:
            d, tried = today, 0
            while True:
                try:
                    raw = self._rows(sym, d)
                except Exception:
                    self.close()
                    raise
                if len(raw):
                    out = build(raw, sym, d)
                    break
                tried += 1
                if tried > self.LOOKBACK:
                    raise ProfileError(f"no profile for {sym} on {today} or the "
                                       f"{self.LOOKBACK} business days before")
                d -= dt.timedelta(days=1)
                while d.weekday() >= 5:
                    d -= dt.timedelta(days=1)
        self._cache[(sym, today)] = out
        return out

    def close(self):
        if self._q is not None:
            try:
                self._q.close()
            except Exception:
                pass
        self._q = None


class MockVprof:
    """A plausible U-shaped profile with auctions, so the modal works without kdb.
    Asian-style session with a lunch break: 09:30-11:30, 13:00-16:00."""

    label = "simulated profile"

    def get(self, sym: str, today: dt.date | None = None) -> dict:
        today = today or dt.date.today()
        rng = np.random.default_rng(zlib.crc32(sym.encode()))   # stable per symbol
        mins = list(range(570, 691, 10)) + list(range(780, 961, 10))
        t = np.linspace(0, 1, len(mins) - 1)
        w = (1.6 - np.sin(np.pi * t)) * rng.uniform(0.8, 1.2, len(t))   # U shape
        cum = np.concatenate([[0.0], np.cumsum(w) / w.sum()])
        adv = float(rng.integers(2, 40)) * 1e6
        head = [(0, adv), (1, adv * rng.uniform(0.02, 0.06)),
                (2, adv * rng.uniform(0.08, 0.25)), (3, adv * rng.uniform(0.002, 0.01))]
        raw = pd.DataFrame({"time": [h[0] for h in head] + mins,
                            "vmed": [h[1] for h in head] + cum.tolist()})
        return build(raw, sym, today)

    def close(self):
        pass
