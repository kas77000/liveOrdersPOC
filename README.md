# Live Orders POC

Live blotter of the algo engine's parent orders, read from the OMS kdb+ database.
Two self-contained versions, one per folder. They share only the Python environment.

| | `poll/` (port 8000) | `realtime/` (port 8001) |
|---|---|---|
| Data path | every second, reads **only the rows appended** to the RDB since the last read | subscribes to the **tickerplant** (`.u.sub`); starting snapshot from the RDB |
| Latency | up to 1 s | ~100 ms (ticks are batched per 100 ms) |
| Load on kdb | 4 tiny reads/s, cost proportional to new rows, not to table size | none per update; one RDB snapshot at start, plus a tiny catch-up read every 10 s |
| Needs | RDB host/port | RDB + tickerplant host/port |

```
poll:      OMS RDB ──new rows every 1s──▶ FastAPI ──WebSocket (changed rows)──▶ React table
realtime:  OMS TP ──.u.upd per tick──▶ FastAPI ──WebSocket (changed rows)──▶ React table
           OMS RDB ──snapshot at start──┘
```

## Setup (once)

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## Run

```bat
poll\run.bat          :: http://localhost:8000
realtime\run.bat      :: http://localhost:8001
```

Without the hosts set in `run.bat`, each version runs on its own simulator, so it can be
demoed anywhere. Both can run at the same time for comparison.

- RDB: `KDB_HOST` / `KDB_PORT` of the OMS, plus `KDB_USER` / `KDB_PASS` if needed
- Tickerplant (realtime only): `TP_HOST` / `TP_PORT`. Check it first with
  `.venv\Scripts\python realtime\scripts\check_tickerplant.py HOST PORT`

Tests, per folder: `cd poll` (or `cd realtime`), then `..\.venv\Scripts\python -m pytest -q`.

The frontend is React 18 + [htm](https://github.com/developit/htm) loaded from
`frontend/vendor/`: no Node, no build step, no CDN. It works on a locked-down desktop.

## How the incremental read works (both versions)

RDB tables are append-only during the day, so the new rows are the ones after the last
position read. Per table, one call:

```q
{[n] c:count target_state; t:select id_server,id_target,make from target_state;
     $[c<n; (1b; t); (0b; (n; c-n) sublist t)]}
```

`sublist` takes rows by position, so kdb never scans the old rows. If a table got shorter
(end-of-day reset or RDB restart), the whole table is reloaded. The server keeps one row per
order in memory. Fills are merged with `max` (fills are cumulative), so a late or replayed
row can never move an order backwards.

## Real-time startup sequence

1. Subscribe to the tables the tickerplant publishes (`.u.t`); updates queue in the socket.
2. Load today's rows from the RDB.
3. Apply the queued and following updates.

This order means no update is missed between the snapshot and the subscription. Tables the
tickerplant doesn't publish (e.g. maybe `target_client`) are refreshed by the incremental
RDB read every `SWEEP_SECONDS` (10 s). On a disconnect, the whole sequence restarts.

## Column sources

| Column | Source |
|---|---|
| Symbol, Side | `target.sym`, `target.side` |
| Limit | `target.limit_price`; `0` shown as MKT |
| Strategy | `target.algo` |
| Account | `target.trader` |
| Start / End Time | `target.t_start` / `t_end` |
| Market | suffix of `target.sym` (`.KS`→KR, `.IJ`→INDO, `.TT`→TW, no suffix→US …) |
| Min % / Max % / Target % | `fixmsg` tag 9012, sub-tags **5 / 6 / 7**. Without them it falls back per algo to `beta`/`delta`, as the engine does. For PART, min = max = target. |
| Flow Type | `target.basket` contains "SILK" → Swap, otherwise Agency |
| Client | `target_client.client` |
| % Filled | `target_state.make` / `target.size` |
| Amount USD | `size × target_stock.adjclose × target_stock.fxlast` (notional at previous close) |

Health, Due and Status are not shown for now. I found no source for Due in the tables.

## Code (same layout in both folders)

- `backend/sources.py`: where data comes from (`KdbSource` incremental reads; `RealtimeSource` in realtime) and the simulator
- `backend/transform.py`: raw rows → blotter rows (pure, unit-tested)
- `backend/fixmsg.py`: fixmsg / 9012 parsing and participation levels
- `backend/app.py`: update loop, diff, WebSocket fan-out, static files
- `frontend/app.js`: the table (sort, filter, row flash on change)
