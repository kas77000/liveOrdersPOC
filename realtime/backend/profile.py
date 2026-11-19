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

    VprofSource   the gateway: get_data_by_date[`profile;...], previous business day
    MockVprof     a plausible profile, for running without kdb
    final_profile the transformation; pure, unit-tested
"""
from __future__ import annotations

import datetime as dt
import logging
import math
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


# --------------------------------------------------------------------------- order
def order_profile(full: dict, start: int, end: int, include_open: bool = True,
                  include_close: bool = True) -> dict:
    """The full-day profile cut to an order's window and rescaled to 100% of the ORDER.

    Same idea as ai3's CustomProfile.toMap (the live algo): read the day's cumulative
    curve at the window's start and end; v12 = cp(end) - cp(start) is the share of the
    day's volume the window holds, and the order's schedule is the curve between them
    rescaled to run 0 -> 1. A bucket cut by the window keeps the overlapping part only
    (linear inside the bucket, as q's interp). (toMap weights its start point with the
    end time and vice versa; that looks like a swapped argument and is not copied.)

    Auctions: the open counts when the window covers the open (starts at or before it
    and ends after it) and the order does the open (ai3's includeOpen =
    doOpenScheduler); the close likewise when the window covers the close; the PM reopen auction when the window spans the lunch reopen.

    start / end are minutes on the region clock, like the profile.
    """
    if end <= start:
        raise ProfileError("the window ends before it starts")
    o, c, lunch = full["open_min"], full["close_min"], full["lunch"]
    rows = []
    for r in full["rows"]:
        ph = r["phase"]
        if ph == "open auction":
            take = 1.0 if (include_open and start <= o < end) else 0.0   # window reaches the open
            a, b = r["start"], r["end"]
        elif ph == "close auction":
            take = 1.0 if (include_close and start < c <= end) else 0.0  # ...the close
            a, b = r["start"], r["end"]
        elif ph == "pm auction":
            take = 1.0 if (lunch and start <= lunch[1] < end) else 0.0
            a, b = r["start"], r["end"]
        else:
            a, b = max(r["start"], start), min(r["end"], end)
            take = (b - a) / (r["end"] - r["start"]) if b > a else 0.0
        if take > 0 and r["share"] > 0:
            rows.append(dict(start=a, end=b, label=r["label"] if ph != "continuous" else clock(a),
                             phase=ph, day_share=r["share"] * take))
    v12 = sum(r["day_share"] for r in rows)
    if v12 <= 0:
        raise ProfileError(f"no expected volume between {clock(start)} and {clock(end)} - "
                           "the window misses the trading session")
    total = 0.0
    for r in rows:
        r["share"] = r["day_share"] / v12
        total += r["share"]
        r["cum"] = total
    return dict(sym=full["sym"], date=full["date"], start=start, end=end,
                open_included=any(r["phase"] == "open auction" for r in rows),
                close_included=any(r["phase"] == "close auction" for r in rows),
                v12=v12, expected_volume=v12 * full["vtot"], vtot=full["vtot"],
                open_min=o, close_min=c, lunch=lunch, rows=rows)


# --------------------------------------------------------------------------- tilt
# Tag 148 (a 9012 sub-tag, "alpha,beta[,mode]") front- or back-loads the schedule.
# Replayed from ai3 ProfileSnapshot.setVolumeFrac / tilt(); line numbers below are
# ai3/src/com/kas/ai/ProfileSnapshot.java (transcribed from the decompiled class).

def parse_tag148(value) -> dict | None:
    """'0.5,0.3' or '-0.4,0.25,1' -> {alpha, beta, mode}; None when absent or unreadable
    (Parameters.__p_148: alpha, beta, optional mode)."""
    if value is None:
        return None
    parts = [p.strip() for p in str(value).split(",")]
    try:
        alpha, beta = float(parts[0]), float(parts[1])
        mode = int(float(parts[2])) if len(parts) > 2 and parts[2] else 0
    except (IndexError, ValueError):
        return None
    return dict(alpha=alpha, beta=beta, mode=mode)


def tilt_active(alpha: float, beta: float) -> bool:
    """ProfileSnapshot ctor (:157): |alpha| > 1e-5 and 0 < beta < 1, else no tilt."""
    return abs(alpha) > 1.0e-5 and 0.0 < beta < 1.0


def _trading_minutes(a: float, b: float, lunch) -> float:
    """ProfileSnapshot.deltaTime (:221): b - a without the lunch break."""
    if b <= a:
        return 0.0
    if not lunch or lunch[1] <= lunch[0] or b <= lunch[0] or a >= lunch[1]:
        return b - a
    l1, l2 = lunch
    if a < l1:
        return b - a - (l2 - l1) if b > l2 else l1 - a
    return b - l2 if b > l2 else 0.0


def _jdiv(n: float, d: float) -> float:
    """Java double division: x/0 is +-inf, 0/0 is NaN."""
    if d != 0:
        return n / d
    return math.nan if n == 0 else math.copysign(math.inf, n)


def _jmin(a: float, b: float) -> float:
    """Java Math.min: NaN wins."""
    return math.nan if (a != a or b != b) else min(a, b)


def _tilt(v1t: float, v12: float, alpha: float, v1b: float) -> float:
    """ProfileSnapshot.tilt (:488), the amount added to the target volume v1t.
    x = progress through the window's volume, b = progress at the breakpoint.
      alpha > 0: (1-a)x + a*min(1, x/b)                - a share done by the breakpoint
      alpha < 0: remaining (1-|a|)(1-x) + |a|min(1, (1-x)/(1-b)) - a share kept for after it
    """
    x = v1t / v12
    b = v1b / v12
    if alpha > 0.0:
        return alpha * v12 * (_jmin(1.0, _jdiv(x, b)) - x)
    return alpha * v12 * (_jmin(1.0, _jdiv(1.0 - x, 1.0 - b)) - 1.0 + x)


def tilt_order_profile(op: dict, alpha: float, beta: float, mode: int = 0) -> dict:
    """Replay the engine's tag-148 tilt on an order profile (from order_profile).

    The engine recomputes its target at every snapshot; this evaluates the same
    formulas at the end of each bucket of the order's window, with the profile's own
    volumes standing in for the live ones (so v1t is the profile's expected volume
    since the start). It returns the profile with `share` / `cum` replaced by the
    tilted schedule, the untilted values kept as `base_share` / `base_cum`, and a
    `tilt` block (breakpoint, b, notes).

    Not modelled: the moo/moc fill adjustments, TMX scaling, vsc/tsc, halts.
    Times: the window is clipped to the session (open -> close) for t12 / t1t.
    """
    out = dict(op, rows=[dict(r) for r in op["rows"]])
    rows = out["rows"]
    lunch = op.get("lunch")
    info = dict(alpha=alpha, beta=beta, mode=mode, active=tilt_active(alpha, beta), notes=[])
    for r in rows:
        r["base_share"], r["base_cum"] = r["share"], r["cum"]
    out["tilt"] = info
    if not info["active"]:
        info["notes"].append("no tilt: the engine needs |alpha| > 0 and 0 < beta < 1")
        return out
    if abs(alpha) > 1:
        info["notes"].append("|alpha| > 1: the untilted part (1 - |alpha|) goes negative")

    def auction(phase):
        return sum(r["day_share"] for r in rows if r["phase"] == phase)

    vopen, vpm, vclose = auction("open auction"), auction("pm auction"), auction("close auction")
    do_open, do_pm, do_close = vopen > 0, vpm > 0, vclose > 0
    cont = [r for r in rows if r["phase"] == "continuous"]
    if not cont:
        info["notes"].append("no continuous volume in the window: nothing to tilt")
        return out

    def C(t):   # the window's expected continuous volume up to minute t
        tot = 0.0
        for r in cont:
            if t >= r["end"]:
                tot += r["day_share"]
            elif t > r["start"]:
                tot += r["day_share"] * (t - r["start"]) / (r["end"] - r["start"])
        return tot

    v12c = C(math.inf)
    ts = max(op["start"], op["open_min"])
    te = min(op["end"], op["close_min"])
    t12 = _trading_minutes(ts, te, lunch)
    d3 = beta if alpha > 0 else 1.0 - beta                                   # :651
    itb = te - (1.0 - beta) * t12 if alpha > 0 else te - beta * t12           # :655
    pm_in_tilt = False
    if lunch:                                                                 # :656-664
        if itb <= lunch[1]:
            itb = lunch[0] - (lunch[1] - itb)
            pm_in_tilt = do_pm and alpha < 0
        else:
            pm_in_tilt = do_pm and alpha > 0
    itb = round(itb, 2)
    info["breakpoint"] = itb
    if itb < ts:
        info["notes"].append(
            f"the breakpoint lands at {clock(round(itb))}, before the order starts: the engine "
            "moves it back by the lunch length whenever it falls before the lunch END, "
            "even for a window that ends before lunch (ProfileSnapshot.java:657)")

    v1b = None
    for i, r in enumerate(rows):
        if r["phase"] == "close auction":
            r["cum"] = 1.0
            continue
        t = r["end"]
        t1t = _trading_minutes(ts, min(max(t, ts), te), lunch)
        v1t = C(t)
        before = t1t <= t12 * d3                                              # :652
        itb_now = itb if before else 0.0     # the engine resets itb once past it (:644)
        if before or v1b is None:
            v1b = C(itb) if itb > t else v1t                                  # :653, :666
        a = alpha
        v12l, v1bl = v12c, v1b
        vb = v1b if a > 0 else v12c - v1b                                     # :673
        vo = vopm = vc = 0.0
        if mode in (1, 3):                                                    # :677-696
            if do_open:
                v12l += vopen
                v1bl += vopen
                vo = vopen if a > 0 else 0.0
            if do_pm:
                v12l += vpm
                if lunch and itb_now > lunch[1]:
                    v1bl += vpm
            if do_close:
                v12l += vclose
                vc = vclose if a < 0 else 0.0
            vopm = vpm if (pm_in_tilt and before) else 0.0
        if mode > 1:
            a *= v1bl / v12l                                                  # :697
        vo_n, vpm_n, vc_n = vopen, vpm, vclose
        if mode in (1, 3):                                                    # :700-713
            x = abs(a)
            y = 1.0 - x
            x *= v12l / (vb + vo + vopm + vc)
            vo_n, vpm_n, vc_n = vopen * y + vo * x, vpm * y + vopm * x, vclose * y + vc * x
        tl = _tilt(v1t, v12c, a, v1b)
        if tl != tl:
            info["notes"].append(f"{clock(t)}: the tilt is NaN (0/0) - the engine would "
                                 "reject that snapshot; drawn untilted there")
            tl = 0.0
        if before or "b" not in info:      # the values the engine freezes at the breakpoint
            info["b"], info["alpha_eff"] = v1b / v12c, a
        total = v12c + vo_n + vpm_n + vc_n
        done = v1t + tl
        if any(q["phase"] == "open auction" for q in rows[: i + 1]):
            done += vo_n
        if any(q["phase"] == "pm auction" for q in rows[: i + 1]):
            done += vpm_n
        r["cum"] = done / total
    prev = 0.0
    for r in rows:
        r["share"], prev = r["cum"] - prev, r["cum"]
    if any(r["share"] < -1e-12 for r in rows):
        info["notes"].append("the tilted running total dips in places: the engine re-weights "
                             "the auctions differently once past the breakpoint")
    return out


def tilt_full_day(full: dict, alpha: float, beta: float, mode: int = 0) -> dict:
    """The whole-day profile with the tag-148 tilt, as if an order ran from the open to
    the close with every auction: that order's schedule is the day profile itself (its
    share of the order equals the share of the day), so the engine's tilt replays on it
    unchanged. Rows keep the day profile as base_share / base_cum."""
    op = order_profile(full, full["open_min"], full["close_min"], True, True)
    t = tilt_order_profile(op, alpha, beta, mode)
    return dict(full, rows=t["rows"], tilt=t["tilt"], tilt_window=[op["start"], op["end"]])


def region_now_minute(tz_name: str = "Asia/Hong_Kong") -> float:
    """Minutes of day on the region clock (the clock the profile and orders use)."""
    try:
        from zoneinfo import ZoneInfo
        now = dt.datetime.now(ZoneInfo(tz_name))
    except Exception:          # no tz database: the region clock is UTC+8
        now = dt.datetime.utcnow() + dt.timedelta(hours=8)
    return now.hour * 60 + now.minute + now.second / 60


# --------------------------------------------------------------------------- sources
def profile_query(sym: str, date: dt.date) -> str:
    """The gateway call as one string (the gateway refuses pykx's argument form).
    `profile` is the gateway's dataset alias, not a table name. No cc0: the OMS
    VPROF gateway rejects that column (the header skip is derived instead)."""
    if not SYM_RE.fullmatch(sym or ""):
        # sym is spliced into q source: anything but a ticker is refused outright
        raise ProfileError(f"not a symbol: {sym!r}")
    day = f"{date:%Y.%m.%d}"
    return f"get_data_by_date[`profile;`date`sym`vmed`time;{day};{day};`{sym}]"


def previous_business_day(d: dt.date) -> dt.date:
    d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def _is_q_error(e: Exception) -> bool:
    """An error q raised (pykx QError) - as opposed to a connection failure."""
    return type(e).__name__ == "QError"


class VprofSource:
    """The VPROF gateway.

    The profile dated D is built at D's end of day for trading on the next day, so
    today's blotter reads the PREVIOUS business day's profile (checked by hand on the
    gateway). When that day has nothing (a holiday, or a q error for it) it goes back
    further, up to LOOKBACK business days; a connection failure stops at once.
    One handle, serialised, reopened on failure; cached per symbol and day.
    """

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
        res = self._handle()(profile_query(sym, date))
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
            d, errors = previous_business_day(today), []
            for _ in range(self.LOOKBACK):
                try:
                    raw = self._rows(sym, d)
                except Exception as e:
                    if not _is_q_error(e):
                        self.close()
                        raise
                    errors.append(f"{d}: {e}")
                    raw = None
                if raw is not None and len(raw):
                    out = build(raw, sym, d)
                    break
                d = previous_business_day(d)
            else:
                detail = f" (q errors: {'; '.join(errors)})" if errors else ""
                raise ProfileError(f"no profile for {sym} in the {self.LOOKBACK} business "
                                   f"days before {today}{detail}")
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
