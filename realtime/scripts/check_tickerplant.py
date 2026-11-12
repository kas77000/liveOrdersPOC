"""Check that a kdb+ tickerplant is reachable and is publishing.

    python scripts/check_tickerplant.py HOST PORT [--table target_state] [--seconds 15]
                                                  [--user U --password P]

Steps (stops at the first failure, exit code 1):
  1. TCP: is anything listening on HOST:PORT?
  2. kdb: does a q handshake work (1+1)?
  3. tickerplant: does it have .u.t / .u.w, and which tables does it publish?
  4. subscription: subscribe to --table and count the updates received in --seconds.

Read-only. Step 4 adds this script to the tickerplant's subscriber list (.u.w) for the
duration of the test; the tickerplant removes it when the connection closes.
Needs pykx (pip install pykx).
"""
from __future__ import annotations

import argparse
import socket
import sys
import time


def ok(msg):
    print(f"  [OK]   {msg}")


def fail(msg):
    print(f"  [FAIL] {msg}")
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("host")
    ap.add_argument("port", type=int)
    ap.add_argument("--table", default="target_state", help="table to subscribe to")
    ap.add_argument("--seconds", type=float, default=15, help="how long to listen")
    ap.add_argument("--user", default="")
    ap.add_argument("--password", default="")
    a = ap.parse_args()
    auth = dict(username=a.user, password=a.password)

    print(f"1. TCP {a.host}:{a.port}")
    try:
        with socket.create_connection((a.host, a.port), timeout=5):
            ok("port is open")
    except OSError as e:
        fail(f"cannot reach it: {e}  (wrong host/port, firewall, or process down)")

    try:
        import pykx as kx
    except Exception as e:
        fail(f"pykx not importable: {e}  (pip install pykx)")

    print("2. kdb+ handshake")
    try:
        q = kx.SyncQConnection(host=a.host, port=a.port, timeout=10.0, **auth)
        assert q("1+1").py() == 2
        ok(f"q process answers (kdb+ {q('.z.K').py()}, {q('.z.k').py()})")
    except Exception as e:
        fail(f"{type(e).__name__}: {e}  (wrong credentials, or not a q process)")

    print("3. tickerplant")
    try:
        if not q('`u in key `').py() or not q("`t`w in key `.u").py() == [True, True]:
            fail("no .u.t/.u.w here: this is a q process but not a tickerplant "
                 "(an RDB/HDB? ask for the tickerplant's port)")
        tables = [str(t) for t in q(".u.t").py()]
        subs = {str(k): v for k, v in q("count each .u.w").py().items()}
        ok(f"publishes {len(tables)} tables")
        for t in tables:
            print(f"         {t:<20} {subs.get(t, 0)} subscriber(s)")
        try:
            print(f"         message count today (.u.i): {q('.u.i').py()}")
        except Exception:
            pass
    except SystemExit:
        raise
    except Exception as e:
        fail(f"{type(e).__name__}: {e}")
    finally:
        q.close()

    print(f"4. subscribe to {a.table} for {a.seconds:.0f}s")
    if a.table not in tables:
        fail(f"{a.table} is not published by this tickerplant (see list above)")
    n_msgs = n_rows = 0
    cols = None
    try:
        with kx.RawQConnection(host=a.host, port=a.port, **auth) as tp:
            res = tp.send_sync(".u.sub", a.table, "")
            cols = list(res[1].pd().columns)
            ok(f"subscribed; columns: {', '.join(cols)}")
            end = time.monotonic() + a.seconds
            while time.monotonic() < end:
                msg = tp.poll_recv()
                if msg is None:
                    time.sleep(0.01)
                    continue
                n_msgs += 1
                try:
                    n_rows += len(msg[2])
                except Exception:
                    pass
                if n_msgs == 1:
                    print(f"         first message: {str(msg)[:300]}")
    except Exception as e:
        fail(f"{type(e).__name__}: {e}")

    if n_msgs:
        ok(f"received {n_msgs} message(s), {n_rows} row(s) -- the tickerplant is live")
    else:
        print(f"  [WARN] subscribed, but nothing arrived in {a.seconds:.0f}s. Fine if the "
              "market is quiet or closed; otherwise retry with --seconds 60.")


if __name__ == "__main__":
    main()
