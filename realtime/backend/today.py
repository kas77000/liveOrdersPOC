"""Today's traded volume against the volume profile, with a forecast to the close.

Trades come from `qatt` on the QUOTES server (kdb-queries: queries/liquidity_profile):
every qatt row is a transaction, `price>0, size>0` is the whole test for a print, and
`time` is HKT - the same region clock as the VPROF profile - so a trade falls straight
into the profile's buckets:

    before the open          -> OPEN   (the open auction prints before the session)
    inside the lunch gap     -> PM     (the afternoon reopen auction)
    from the close           -> CLOSE  (the closing auction)
    otherwise                -> the 10-minute continuous bucket holding it

Forecast ("damped pace"). E_i is the profile's expected volume in bucket i
(its share of the day x vtot), E_now what the profile expected by now and R what has
traded. Today's pace, pulled towards 1 early in the day as if a tenth of a normal day
had already traded exactly on profile:

    pace = (R + k*E_day) / (E_now + k*E_day)        k = PRIOR_DAY_FRACTION = 0.1

Every bucket still to come - the rest of the current one, the close auction - is
forecast at pace x E_i. Once the close auction has printed there is nothing to forecast.
"""
from __future__ import annotations

import datetime as dt
import threading
import time
import zlib

import numpy as np
import pandas as pd

from .profile import SYM_RE, ProfileError, region_now_minute

PRIOR_DAY_FRACTION = 0.1


# --------------------------------------------------------------------------- query
def qatt_query(sym: str) -> str:
    """Today's prints per minute, as liquidity_profile.q reads them on the quote RDB
    (no date column there: an RDB holds today only). Bucketed in milliseconds, not with
    xbar against a temporal (liquidity_profile's note); size cast to long before the
    sum. The symbol is spliced in, so only a ticker is accepted."""
    if not SYM_RE.fullmatch(sym or ""):
        raise ProfileError(f"not a symbol: {sym!r}")
    return ('{[sy] t:select time,size from qatt where sym=sy, price>0, size>0;'
            ' 0!select trades:count i, shares:sum "j"$size'
            ' by bkt:"t"$60000*("j"$time) div 60000 from t}'
            f'[`$"{sym}"]')


def _minute(v) -> int:
    """kdb time (pandas Timedelta / datetime.time / ms) -> minute of the day."""
    if hasattr(v, "total_seconds"):
        return int(v.total_seconds() // 60)
    if hasattr(v, "hour"):
        return v.hour * 60 + v.minute
    return int(v) // 60000


def minutes_from_frame(df: pd.DataFrame) -> list[tuple[int, int, int]]:
    """(minute, trades, shares) rows, sorted."""
    if df is None or len(df) == 0:
        return []
    out = [(_minute(r["bkt"]), int(r["trades"]), int(r["shares"])) for r in df.to_dict("records")]
    return sorted(out)


# --------------------------------------------------------------------------- compute
def _row_of(rows: list[dict], m: int, o: int, c: int, lunch) -> int:
    """Index of the profile row a trade in minute m belongs to."""
    phase = {r["phase"]: i for i, r in enumerate(rows) if r["phase"] != "continuous"}
    cont = [i for i, r in enumerate(rows) if r["phase"] == "continuous"]
    if m < o and "open auction" in phase:
        return phase["open auction"]
    if m >= c and "close auction" in phase:
        return phase["close auction"]
    if lunch and lunch[0] <= m < lunch[1]:
        if "pm auction" in phase:
            return phase["pm auction"]
        return max((i for i in cont if rows[i]["end"] <= lunch[0]), default=cont[0])
    for i in cont:
        if rows[i]["start"] <= m < rows[i]["end"]:
            return i
    # outside every bucket (an odd print): the nearest continuous bucket
    return min(cont, key=lambda i: min(abs(m - rows[i]["start"]), abs(m - rows[i]["end"])))


def today_vs_profile(full: dict, minutes: list[tuple[int, int, int]], now: float,
                     k: float = PRIOR_DAY_FRACTION) -> dict:
    """Today's actual volume per profile bucket, and the forecast for the rest of it.

    full: the (untilted) whole-day profile from profile.build. minutes: today's
    (minute, trades, shares). now: minute of the day on the region clock.
    Each row: start, end, label, phase, expected, actual, trades, forecast (shares),
    elapsed (0..1 of the bucket gone by).
    """
    rows = [dict(start=r["start"], end=r["end"], label=r["label"], phase=r["phase"],
                 expected=r.get("base_share", r["share"]) * full["vtot"],
                 actual=0, trades=0, forecast=0.0) for r in full["rows"]]
    o, c, lunch = full["open_min"], full["close_min"], full["lunch"]
    for m, n, s in minutes:
        i = _row_of(rows, m, o, c, lunch)
        rows[i]["actual"] += s
        rows[i]["trades"] += n

    for r in rows:
        if r["phase"] == "continuous":
            r["elapsed"] = min(1.0, max(0.0, (now - r["start"]) / (r["end"] - r["start"])))
        else:   # an auction is over once its bucket has ended or it has printed
            r["elapsed"] = 1.0 if (now >= r["end"] or r["actual"] > 0) else 0.0

    close = next((r for r in rows if r["phase"] == "close auction"), rows[-1])
    finished = close["elapsed"] >= 1.0 and now >= close["start"]
    e_day = sum(r["expected"] for r in rows)
    e_now = sum(r["expected"] * r["elapsed"] for r in rows)
    traded = sum(r["actual"] for r in rows)
    raw = traded / e_now if e_now > 0 else None
    pace = (traded + k * e_day) / (e_now + k * e_day) if e_day > 0 else 1.0
    if not finished:
        for r in rows:
            r["forecast"] = pace * r["expected"] * (1.0 - r["elapsed"])
    forecast = sum(r["forecast"] for r in rows)
    return dict(sym=full["sym"], profile_date=full["date"], now_min=now, finished=finished,
                traded=traded, trades=sum(r["trades"] for r in rows),
                expected_now=e_now, expected_day=e_day, pace_raw=raw, pace=pace,
                prior_day_fraction=k, forecast_rest=forecast, forecast_day=traded + forecast,
                rows=rows)


# --------------------------------------------------------------------------- sources
class QattSource:
    """qatt on the QUOTES real-time server. One handle, serialised, reopened on
    failure; a symbol's minutes are cached for TTL seconds (the chart refreshes)."""

    TTL = 10.0

    def __init__(self, host: str, port: int, user: str = "", password: str = ""):
        self.host, self.port, self.user, self.password = host, port, user, password
        self._q = None
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, list]] = {}

    label = property(lambda self: f"qatt {self.host}:{self.port}")

    def get(self, sym: str, full: dict | None = None, now: float | None = None) -> list[tuple[int, int, int]]:
        hit = self._cache.get(sym)
        if hit and time.monotonic() - hit[0] < self.TTL:
            return hit[1]
        with self._lock:
            try:
                if self._q is None:
                    import pykx as kx
                    self._q = kx.SyncQConnection(host=self.host, port=self.port,
                                                 username=self.user, password=self.password,
                                                 timeout=15.0, no_ctx=True)
                res = self._q(qatt_query(sym))
                # unlicensed pykx: convert before touching it
                out = minutes_from_frame(res.pd())
            except Exception:
                self.close()
                raise
        self._cache[sym] = (time.monotonic(), out)
        return out

    def close(self):
        if self._q is not None:
            try:
                self._q.close()
            except Exception:
                pass
        self._q = None


class MockQatt:
    """Simulated prints that follow the (simulated) profile at a pace of its own,
    up to now, so the chart and the forecast can be seen without kdb."""

    label = "simulated trades"

    def __init__(self, tz: str = "Asia/Hong_Kong"):
        self.tz = tz

    def get(self, sym: str, full: dict | None = None, now: float | None = None) -> list:
        if full is None:
            return []
        now = region_now_minute(self.tz) if now is None else now
        seed = zlib.crc32(f"{sym}{dt.date.today()}".encode())
        rng = np.random.default_rng(seed)
        pace = rng.uniform(0.6, 1.6)                    # busier or quieter than usual
        out = []
        for r in full["rows"]:
            vol = r["share"] * full["vtot"] * pace
            if r["phase"] != "continuous":              # one print for the auction
                m = r["start"] + 9 if r["phase"] != "close auction" else r["start"] + 8
                if m < now:
                    out.append((int(m), 1, int(vol * rng.uniform(0.7, 1.3))))
                continue
            for m in range(int(r["start"]), int(r["end"])):
                if m >= now:
                    break
                v = vol / (r["end"] - r["start"]) * rng.lognormal(0, 0.5)
                out.append((m, int(rng.integers(5, 60)), int(v)))
        return out

    def close(self):
        pass
