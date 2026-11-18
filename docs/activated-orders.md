# Activated orders

## What the user sees

- By default the blotter lists only orders whose **latest `target_state.state` is
  `activated`**, i.e. orders the algo is still working.
- **Show all states** (header checkbox) lists every order of the day: `filled`, `cxl`,
  `rejected`, `created`, …
- **State** is an advanced column (tick **Show advanced information**). The search box
  also matches it.
- The header pill reads `N activated · M orders`.

## How it is done

| Step | ai3 / kdb-queries | liveOrdersPOC |
|---|---|---|
| Where the state lives | `target_state.state` (symbol), many rows per order, in time order | same table, read incrementally from the OMS RDB |
| Latest state per order | `select state:last state … by id_target from target_state` (`kdb-queries/queries/basket_vs_close/basket_vs_close.q:6`, `queries/limit_up_down/limit_up_down.q:34`) | in q: `state:last state by id_server,id_target` on each batch of new rows (`poll/backend/sources.py:40-55`); then in the cache, `last` across batches (`poll/backend/sources.py:81-83`) |
| The fill | `make` | `make:max make`: a cumulative fill, so a late or replayed row can never move it backwards |
| "Still working" | `` where state=`activated `` (`basket_vs_close.q:10`, `stale_price_check.q:82`, `limit_up_down.q:36`) | `r.state === "activated"` (`poll/frontend/app.js:17`, `:129`) |
| To the browser | — | each blotter row carries `state` (`poll/backend/transform.py:82`, `:117`) |

### Why "last" is the latest

The RDB tables are append-only during the day, so rows arrive in time order. The
incremental read takes the rows after the ones already seen (by position), collapses
them per order in q (`last state`), and the cache merges each batch onto what it holds
by keeping the newest row. This gives the same answer as `last state` over the whole table.

### Edge cases

- **An order with no `target_state` row yet** has no state, so it is not `activated`
  and is hidden until its first state row arrives (the kdb-queries scripts behave the
  same: a missing state never equals `` `activated ``).
- **`target_state` unreadable** (missing table or column): no order has a state, and
  the filter is switched off rather than hiding everything (`haveStates`,
  `poll/frontend/app.js:119-129`). The read failure is logged.
- `Show all states` is not remembered; the page always opens on activated orders.

## How to check it by hand

On the OMS RDB, for one order:

```q
select state:last state, make:max make by id_server,id_target from target_state where id_target=<id>
```

The blotter shows that order by default only if `state` is `` `activated ``.
