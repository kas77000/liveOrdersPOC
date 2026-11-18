# Order Volume Profile

Second tab of the order modal. It answers: **given the order's window, how should its
quantity spread over time, and where should it be now?** It takes the
[Full Day Volume Profile](full-day-volume-profile.md), cuts it to the order's start →
end, and rescales it so the order's buckets sum to 100% of the order. This is what the
live algo computes in `CustomProfile.toMap` (`ai3/src/com/kas/ai/CustomProfile.java:39-80`),
called by `AlgorithmDispatcher` with the order's start, end and `doOpenScheduler` flag
(`ai3/src/com/kas/ai/AlgorithmDispatcher.java:345`).

## 1. What ai3 computes

`CustomProfile` holds two cumulative day curves for the symbol: `_cpOpts` (with the
auctions) and `_cpExAucOpts` (without). `toMap` picks one with `includeOpen`
(= the order's `doOpenScheduler`), reads it at three times, and returns:

| ai3 value | Formula (`CustomProfile.java:63-67`) | Meaning |
|---|---|---|
| `v01` | `cp(start)` | share of the day traded before the order starts |
| `v12` | `cp(end) − cp(start)` | **share of the day's volume inside the order's window** |
| `v1t` | `cp(now) − cp(start)` | share of the day traded so far inside the window |
| `vt2` | `v12 − v1t` | share of the day still to trade inside the window |

`v1t / v12` is therefore **how far through its schedule the order should be now**.
`getEstVolume` (`ai3/src/com/kas/ai/kdb/vproflib.q:1011-1019`) does the same in q:
`.t.b: interp[...;(t0;t1)]`, then `v12: (.t.b[1]-.t.b[0])*(vtot…)` turns the window's
share into shares.

## 2. How liveOrdersPOC does it

`order_profile(full, start, end, include_open, include_close)`
(`poll/backend/profile.py:176-227`), from the cached full-day rows:

| # | Step | Detail |
|---|---|---|
| 1 | Window | `start`, `end` in minutes on the region clock; default `target.t_start` / `t_end`. `end ≤ start` is refused. |
| 2 | Continuous buckets | each bucket keeps the part of it inside the window: a bucket cut by the window counts `overlap / bucket length` of its share (the curve is linear inside a bucket, as `interp` reads it). A 08:25 start takes half of the 08:20–08:30 bucket. |
| 3 | Open auction | counted only if the window **covers the open** (`start ≤ open < end`) **and** the order does the open (`doopen`, ai3's `includeOpen`) |
| 4 | Close auction | counted only if the window **covers the close** (`start < close ≤ end`) **and** `doclose` |
| 5 | PM reopen auction | counted when the window spans the lunch reopen (`start ≤ reopen < end`) |
| 6 | `v12` | sum of what was kept = `cp(end) − cp(start)` on the curve with the auctions chosen in 3–5 |
| 7 | Order schedule | each kept bar ÷ `v12` → share **of the order**; running total ends at 100% |
| 8 | Expected market volume | `v12 × vtot` (ai3's `v12` in shares) |
| 9 | Now | the current minute on `REGION_TZ` (default Asia/Hong_Kong). Expected progress = running total at now (= `v1t / v12`); filled = `make / size` from the blotter, live |

`GET /api/order_profile?sym=&start=&end=&open=&close=` (`poll/backend/app.py:136`)
returns the rows plus `v12`, `expected_volume`, which auctions were included and `now_min`.
The tab is `OrderTab` (`poll/frontend/profile.js:258`); "expected by now" is `cumAt`
(`poll/frontend/profile.js:74`).

### Mapping to ai3's names

| On screen | ai3 |
|---|---|
| Share of the day in window | `v12` |
| Expected market volume | `v12` in shares (`getEstVolume`'s `v12`) |
| Expected by now | `v1t / v12` |
| Remaining (100% − expected by now) | `vt2 / v12` |
| Order / expected volume | order size ÷ `v12`-in-shares: the participation the schedule implies |

## 3. Deliberate differences from ai3

1. **Start and end are interpolated at their own times.** In `toMap`
   (`CustomProfile.java:54-55`, `:60-61`) the start point is weighted with `endtime`
   and the end point with `starttime`:
   `vp_start = weighted_vprof(idx_start, endtime, …)`. That looks like a swapped
   argument; when start or end falls between two profile points, ai3's `v12` is
   then off by up to one bucket. Here both are read at their own time (plain linear
   interpolation, like q's `interp`).
2. **Open and close are decided separately.** ai3 chooses between "all auctions" and
   "no auctions" with the single `includeOpen` flag. Here the open follows `doopen` and
   the close follows `doclose`, and either only counts if the window actually covers
   it. (An order entirely before the open never gets the open auction.)
3. **The PM reopen auction** is part of the day (see the full-day page) and counts
   when the window spans the reopen.

## 4. Editing the window

- **Start / End**: 24-hour `HH:MM` on the region clock. A valid time is applied as it
  is typed; an invalid one is shown in red and not applied.
- **Open auction / Close auction**: default to the order's `doopen` / `doclose`
  (missing → yes). Ticking one has no effect unless the window covers that auction.
- **Reset to order** restores the order's own window and flags; "edited — not the
  order's own window" shows while they differ.
- Nothing is written back to kdb: editing is a what-if on the schedule.

## 5. Worked example: 7203.JP, window 08:25 → 13:10 (505 → 790)

Same profile as the full-day example (`vtot` = 24,038,400). The window starts after
the open and ends before the close, so neither auction counts; it spans the lunch
reopen at 690, so the PM auction does.

| | Value |
|---|---|
| `v12` (share of the day in the window) | **41.88%** |
| Expected market volume | 0.4188 × 24,038,400 = **10,066,365** shares |
| First bar 08:25–08:30 | half of the 08:20–08:30 bucket: 1.89% of the day → 1.89 / 41.88 = **4.52% of the order** |
| Second bar 08:30–08:40 | 2.84% of the day → **6.77% of the order** (running 11.29%) |
| Last bar 13:00–13:10 | **3.17% of the order** (running **100%**) |
| Expected by 10:00 (600) | **48.98%** of the order |

An order of 500,000 shares in this window would mean about 5.0% of the expected
market volume, and should be about 245,000 shares done by 10:00.

## 6. How to check it by hand

1. Build the full-day bars (full-day page, section 6).
2. Keep the bars inside `[start, end)`; for a bar the window cuts, keep the
   overlapping fraction.
3. Keep OPEN / CLOSE / PM only under the rules in section 2.
4. `v12` = the sum; each bar's order share = its kept share ÷ `v12`.
5. Compare `v12` with ai3's value for the same order: `AlgorithmDispatcher` puts the
   `toMap` result in its order map under the key `<sym>:sm_v12`
   (`AlgorithmDispatcher.java:345-348`). Expect small gaps only where start or end
   falls inside a bucket (section 3, point 1).
