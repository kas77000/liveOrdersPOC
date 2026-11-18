"""Where the raw frames come from (polling version).

  KdbSource   reads only the rows appended to the OMS RDB since the last poll
  MockSource  simulator for demos without kdb
Every source exposes fetch() -> frames for transform.build_rows, and wait(timeout),
which returns when it is worth calling fetch() again.
"""
from __future__ import annotations

import logging
import random
import threading
import time

import pandas as pd

log = logging.getLogger("liveorders")

KEY = ["id_server", "id_target"]

# Only the columns the blotter needs -- target_stock alone has 94.
COLUMNS = {
    "target": ["id_server", "id_target", "sym", "side", "size", "limit_price", "algo", "beta",
               "delta", "t_start", "t_end", "trader", "basket", "fixmsg", "doopen", "doclose"],
    "target_state": ["id_server", "id_target", "make", "state"],
    "target_client": ["id_server", "id_target", "client"],
    "target_stock": ["id_server", "id_target", "adjclose", "orgclose", "fxlast"],
}
TABLES = list(COLUMNS)

# RDB tables are append-only during the day, so the rows after the ones already seen
# are exactly the new ones; `sublist` takes them by position. They are then collapsed
# to one row per order inside q -- the same merge OrderCache does -- so the first load
# of the day ships one row per order instead of every tick.
# Returns (reset; count; rows). If the table got shorter (end-of-day reset or RDB
# restart) the whole table comes back with reset=1b.
# Built only from forms known to work on the OMS RDB: the original sublist query and
# `0!select c:agg c by key from local` (as in kdb-queries). Locals have names no
# column will have, since inside a select a column shadows a local.
INCREMENTAL_Q = (
    "{{[n] lqCnt:count {t}; lqRst:lqCnt<n; lqBeg:$[lqRst;0;n]; "
    "lqTbl:(lqBeg; lqCnt-lqBeg) sublist select {cols} from {t}; "
    "lqTbl:0!select {agg} by id_server,id_target from lqTbl; "
    "(lqRst; lqCnt; lqTbl)}}"
)


def _agg(table: str) -> str:
    """Per-table collapse; must agree with OrderCache.apply: make is the max (a
    cumulative fill), everything else - target_state.state included - the last row."""
    return ",".join(f"{c}:{'max' if c == 'make' else 'last'} {c}"
                    for c in COLUMNS[table] if c not in KEY)


AGG = {t: _agg(t) for t in COLUMNS}


class OrderCache:
    """One row per order per table, merged as new rows arrive. Thread-safe."""

    def __init__(self):
        self._lock = threading.Lock()
        self._frames = {t: pd.DataFrame(columns=COLUMNS[t]) for t in TABLES}

    def reset(self, table: str):
        with self._lock:
            self._frames[table] = pd.DataFrame(columns=COLUMNS[table])

    def apply(self, table: str, rows: pd.DataFrame):
        if rows is None or rows.empty:
            return
        rows = rows[[c for c in COLUMNS[table] if c in rows.columns]]
        with self._lock:
            old = self._frames[table]
            both = rows if old.empty else pd.concat([old, rows], ignore_index=True)
            if table == "target_state":
                # make is a cumulative fill: max is order-independent, so a late or
                # replayed row can never move an order backwards.
                # state is the LATEST row (rows arrive in time order), as the
                # kdb-queries scripts read it: `select state:last state by ...`.
                agg = {c: ("max" if c == "make" else "last")
                       for c in ("make", "state") if c in both.columns}
                both = both.groupby(KEY, as_index=False, sort=False).agg(agg)
            else:
                both = both.groupby(KEY, as_index=False, sort=False).last()
            self._frames[table] = both

    def frames(self) -> dict[str, pd.DataFrame]:
        with self._lock:
            return dict(self._frames)


def _to_df(x) -> pd.DataFrame:
    df = x.pd() if hasattr(x, "pd") else pd.DataFrame(x)
    df = df.reset_index() if df.index.names[0] is not None else df
    # q strings (char vectors) arrive as bytes; the transform expects str.
    for c in df.columns:
        if df[c].dtype == object and df[c].map(lambda v: isinstance(v, bytes)).any():
            df[c] = df[c].map(lambda v: v.decode("utf-8", "replace") if isinstance(v, bytes) else v)
    return df



# --------------------------------------------------------------------------- poll
class KdbSource:
    """Polls the OMS RDB incrementally. Read-only; one handle, reopened on failure."""

    def __init__(self, host: str, port: int, user: str = "", password: str = "",
                 poll_seconds: float = 1.0):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.poll_seconds = poll_seconds
        self.cache = OrderCache()
        self.seen = {t: 0 for t in TABLES}       # rows already read, per table
        self._q = None
        self._broken: set[str] = set()

    label = property(lambda self: f"kdb poll {self.host}:{self.port}")

    def _handle(self):
        if self._q is None:
            import pykx as kx
            self._q = kx.SyncQConnection(host=self.host, port=self.port,
                                         username=self.user, password=self.password,
                                         timeout=10.0)
        return self._q

    def _read_new(self, table: str, n: int) -> tuple[bool, int, pd.DataFrame]:
        """(reset?, table row count, new rows collapsed per order). Overridden in tests."""
        q = INCREMENTAL_Q.format(t=table, agg=AGG[table], cols=",".join(COLUMNS[table]))
        reset, count, rows = _to_list(self._handle()(q, n))[:3]
        return bool(reset), int(count), _to_df(rows)

    def sync(self, tables=TABLES):
        """Pull new rows for `tables` into the cache."""
        try:
            for table in tables:
                try:
                    reset, count, rows = self._read_new(table, self.seen[table])
                except Exception as e:   # missing table/column: degrade, don't die
                    if table == "target":
                        raise
                    if table not in self._broken:
                        log.warning("reading %s failed (%s) -- its columns show '—'", table, e)
                        self._broken.add(table)
                    continue
                self._broken.discard(table)
                if reset:
                    log.info("%s shrank (RDB reset?) -- reloading it", table)
                    self.cache.reset(table)
                self.seen[table] = count
                self.cache.apply(table, rows)
        except Exception:
            self.close()
            raise

    def fetch(self) -> dict[str, pd.DataFrame]:
        self.sync()
        return self.cache.frames()

    def wait(self, timeout: float):
        time.sleep(max(0.0, timeout))

    def close(self):
        if self._q is not None:
            try:
                self._q.close()
            except Exception:
                pass
        self._q = None


# --------------------------------------------------------------------------- mocks
_SYMS = [("005930.KS", 71000, 0.00072), ("000660.KS", 182000, 0.00072),
         ("7203.JP", 2850, 0.0066), ("6758.JP", 3200, 0.0066), ("9984.JP", 8900, 0.0066),
         ("RELIANCE.IN", 2900, 0.012), ("INFY.IN", 1650, 0.012), ("BBCA.IJ", 9800, 0.000063),
         ("2330.TT", 1050, 0.031), ("2317.TT", 210, 0.031), ("0700.HK", 420, 0.128),
         ("BHP.AU", 44, 0.66), ("AAPL", 228, 1.0), ("MSFT", 415, 1.0), ("NVDA", 140, 1.0)]
_CLIENTS = ["Cobalt Funds", "Eastgate Fund", "Bright Pension", "Delta Asset Mgmt",
            "Aurora Capital", "Northwind Partners"]
_ALGOS = [("VWAP", "9001=VWAP;9012=5=3^6=12"), ("PART", "9001=PART;9012=7=10"),
          ("LINE", "9001=LINE;9012=5=4^6=12^15=1"),
          ("NINJA", "9001=NINJA;9012=6=20"), ("SHADOW", "9001=SHADOW;9012=5=2"),
          ("TMX", "9001=TMX;9012=92=A,5^6=8"), ("DMA", "9001=DMA"),
          ("CLOSE", "9001=CLOSE"), ("DARK", "9001=DARK")]


class MockSource:
    """Simulated RDB for the poll version: orders arrive and fill over time."""

    label = "mock poll (simulated)"

    def __init__(self, n_orders: int = 30, seed: int = 7):
        self.rng = random.Random(seed)
        self.next_id = 100700
        self.target, self.state, self.client, self.stock = [], [], [], []
        self._lock = threading.Lock()
        for _ in range(n_orders):
            self._new_order()

    def _new_order(self):
        r = self.rng
        sym, px, fx = r.choice(_SYMS)
        algo, fixmsg = r.choice(_ALGOS)
        self.next_id += 1
        key = dict(id_server=r.choice([1, 2]), id_target=self.next_id)
        # 09:30-11:15 or 13:00-14:45: inside the simulated profile's session, never at lunch
        start = pd.Timedelta(minutes=r.choice(list(range(570, 690, 15)) + list(range(780, 900, 15))))
        self.target.append(dict(
            **key, sym=sym, side=r.choice(["buy", "sell"]),
            size=r.choice([500, 1000, 5000, 20000, 100000]),
            limit_price=0.0 if r.random() < 0.3 else round(px * r.uniform(0.97, 1.03), 2),
            algo=algo, beta=0.0, delta=0.0, t_start=start,
            t_end=min(start + pd.Timedelta(hours=r.choice([1, 2, 4, 7])), pd.Timedelta(minutes=960)),
            doopen=int(r.random() < 0.8), doclose=int(r.random() < 0.8),
            trader=r.choice(["jdoe", "kwong", "msato"]),
            basket=r.choice(["SILK_KR01", "BASK_A", "BASK7", "SILKAP"]), fixmsg=fixmsg))
        self.client.append(dict(**key, client=r.choice(_CLIENTS)))
        self.stock.append(dict(**key, adjclose=px, orgclose=px, fxlast=fx))
        live = r.random() > 0.2   # ~1 in 5 simulated orders is not working
        self.state.append(dict(**key, make=0, live=live,
                               state="activated" if live else r.choice(["cxl", "created", "rejected"])))

    def _fill(self, s, t):
        s["make"] = min(t["size"], s["make"] + max(1, int(t["size"] * self.rng.uniform(0.01, 0.06))))
        if s["make"] >= t["size"]:
            s["state"] = "filled"

    def step(self):
        """One second of market activity."""
        for s, t in zip(self.state, self.target):
            if s["live"] and s["make"] < t["size"] and self.rng.random() < 0.25:
                self._fill(s, t)
        if self.rng.random() < 0.08:
            self._new_order()

    def frames(self) -> dict[str, pd.DataFrame]:
        with self._lock:
            return {"target": pd.DataFrame(self.target),
                    "target_state": pd.DataFrame(self.state).drop(columns="live"),
                    "target_client": pd.DataFrame(self.client),
                    "target_stock": pd.DataFrame(self.stock)}

    def fetch(self) -> dict[str, pd.DataFrame]:
        with self._lock:
            self.step()
        return self.frames()

    def wait(self, timeout: float):
        time.sleep(max(0.0, timeout))

    def close(self):
        pass


def _to_list(x):
    if hasattr(x, "py"):
        try:
            return x.py()
        except Exception:
            return list(x)
    return x
