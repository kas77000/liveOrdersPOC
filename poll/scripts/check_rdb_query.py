"""Run the blotter's RDB query against a real server and say exactly which part fails.

    python scripts/check_rdb_query.py HOST PORT [--user U --password P]

For each table the app reads, runs the full incremental query (as the app does, from
row 0). If it fails, re-runs it one piece at a time -- count, select, each column,
sublist, the per-order collapse -- and prints the q error of the first piece that
breaks. Read-only. Needs pykx; works in unlicensed mode.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.sources import AGG, COLUMNS, INCREMENTAL_Q  # noqa: E402


def run(h, label, q, *args):
    try:
        res = h(q, *args)
        try:
            shown = res.py()
        except Exception:
            shown = "<ok>"
        print(f"  [OK]   {label}: {str(shown)[:100]}")
        return True
    except Exception as e:
        print(f"  [FAIL] {label}: {type(e).__name__}: {e}")
        print(f"         q: {q}")
        return False


def check_table(h, t):
    cols = ",".join(COLUMNS[t])
    full = INCREMENTAL_Q.format(t=t, agg=AGG[t], cols=cols)
    print(f"\n{t}")
    if run(h, "full query from row 0 -> count of rows", f"{{count last {full} x}}", 0):
        return True
    steps = [
        ("count", f"count {t}"),
        ("select the columns", f"count select {cols} from {t}"),
        ("sublist", f"count (0; 5) sublist select {cols} from {t}"),
        ("collapse per order",
         f"count 0!select {AGG[t]} by id_server,id_target from (0; 5) sublist select {cols} from {t}"),
        ("lambda with a local", f"{{[n] lqCnt:count {t}; lqCnt+n}}", 0),
    ]
    for label, q, *args in steps:
        if not run(h, label, q, *args):
            if label == "select the columns":
                for c in COLUMNS[t]:
                    run(h, f"  column {c}", f"count select {c} from {t}")
            break
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("host")
    ap.add_argument("port", type=int)
    ap.add_argument("--user", default="")
    ap.add_argument("--password", default="")
    a = ap.parse_args()

    import pykx as kx
    print(f"pykx {kx.__version__}, licensed={getattr(kx, 'licensed', '?')}")
    h = kx.SyncQConnection(host=a.host, port=a.port, username=a.user,
                           password=a.password, timeout=30.0)
    run(h, "server", "(.z.K; .z.k)")
    ok = all([check_table(h, t) for t in COLUMNS])
    h.close()
    print("\nall queries OK" if ok else "\nsome queries FAILED -- paste this output back")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
