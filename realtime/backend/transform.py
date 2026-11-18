"""Turn raw kdb frames into blotter rows. Pure pandas -- no I/O, unit-tested.

Inputs (one frame per table, today's rows only):
    target        id_server,id_target,sym,side,size,limit_price,algo,beta,delta,
                  t_start,t_end,trader,basket,fixmsg
    target_state  id_server,id_target,make,state      (time-ordered, many rows/order)
    target_client id_server,id_target,client
    target_stock  id_server,id_target,adjclose,orgclose,fxlast
Any of the last three may be empty/missing columns; the blotter degrades to "—".
"""
from __future__ import annotations

import math

import pandas as pd

from .fixmsg import parse_fixmsg, participation

KEY = ["id_server", "id_target"]

# sym suffix (Bloomberg-style composite code) -> market label. Unknown suffixes
# are shown as-is; no suffix = US.
MARKET = {
    "KS": "KR", "KQ": "KR", "TT": "TW", "TW": "TW", "IJ": "INDO", "JP": "JP", "JT": "JP",
    "T": "JP", "IN": "IN", "IB": "IN", "IS": "IN", "HK": "HK", "AU": "AU", "AT": "AU",
    "C1": "CN", "C2": "CN", "CH": "CN", "SP": "SG", "MK": "MY", "TB": "TH", "PM": "PH",
    "NZ": "NZ", "US": "US",
}


def market_of(sym) -> str:
    s = str(sym or "")
    if "." not in s:
        return "US"
    suffix = s.rsplit(".", 1)[1].upper()
    return MARKET.get(suffix, suffix)


def flow_type_of(basket) -> str:
    """Baskets named *SILK* are swap flow; everything else is agency."""
    return "Swap" if "SILK" in str(basket or "").upper() else "Agency"


def _clean(v):
    """JSON-safe scalar: NaN/NaT -> None, numpy -> python, timedelta -> HH:MM:SS."""
    if v is None:
        return None
    if isinstance(v, pd.Timedelta):
        if pd.isna(v):
            return None
        s = int(v.total_seconds())
        return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"
    if isinstance(v, (pd.Timestamp,)):
        return None if pd.isna(v) else v.strftime("%H:%M:%S")
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(v, "item"):
        v = v.item()
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def _last_by_key(df: pd.DataFrame | None, cols: list[str]) -> pd.DataFrame:
    if df is None or df.empty or not set(KEY).issubset(df.columns):
        return pd.DataFrame(columns=KEY + cols)
    have = [c for c in cols if c in df.columns]
    out = df.groupby(KEY, as_index=False, sort=False)[have].last()
    for c in cols:
        if c not in out.columns:
            out[c] = None
    return out


def build_rows(target, target_state=None, target_client=None, target_stock=None) -> list[dict]:
    if target is None or target.empty:
        return []
    t = target.groupby(KEY, as_index=False, sort=False).last()   # last amendment wins
    t = t.merge(_last_by_key(target_state, ["make", "state"]), on=KEY, how="left")
    t = t.merge(_last_by_key(target_client, ["client"]), on=KEY, how="left")
    t = t.merge(_last_by_key(target_stock, ["adjclose", "orgclose", "fxlast"]), on=KEY, how="left")

    rows = []
    for r in t.to_dict("records"):
        g = lambda c: _clean(r.get(c))  # noqa: E731
        tags = parse_fixmsg(g("fixmsg"))
        part = participation(g("algo"), tags, g("beta"), g("delta"))
        size = g("size") or 0
        make = g("make") or 0
        limit = g("limit_price")
        prev = g("adjclose") or g("orgclose")
        fx = g("fxlast")
        amount = size * prev * fx if (size and prev and fx) else None
        rows.append({
            "key": f"{g('id_server')}:{g('id_target')}",
            "id_target": g("id_target"),
            "sym": g("sym"),
            "side": g("side"),
            "limit": limit if limit and limit > 0 else None,      # None -> MKT
            "strategy": g("algo"),
            "account": g("trader"),
            "start": g("t_start"),
            "end": g("t_end"),
            "market": market_of(g("sym")),
            "min_pct": part["min"],
            "max_pct": part["max"],
            "target_pct": part["target"],
            "flow_type": flow_type_of(g("basket")),
            "client": g("client"),
            "size": size,
            "filled": make,
            "pct_filled": round(100.0 * make / size, 1) if size else None,
            "amount_usd": round(amount) if amount else None,
            "state": str(g("state")) if g("state") is not None else None,   # latest target_state
            # does the order take part in the auctions (null -> assume yes, as core-vp)
            "doopen": None if g("doopen") is None else bool(g("doopen")),
            "doclose": None if g("doclose") is None else bool(g("doclose")),
        })
    return rows
