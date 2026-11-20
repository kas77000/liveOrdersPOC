# Full Day Volume Profile

First tab of the order modal (click a row). It shows how the symbol's volume is
expected to spread over a whole trading day — **open auction, the continuous session
in 10-minute buckets, the PM reopen auction (lunch-break markets), close auction** —
each as a share of the day's total volume. It follows ai3's `getsymprof`
(`ai3/src/com/kas/ai/kdb/getVolumeProfileReport.q:36-65`).

## 1. The data: what the VPROF gateway returns

```q
get_data_by_date[`profile;`date`sym`vmed`time;2026.11.17;2026.11.17;`402340.KS]
```

`profile` is the gateway's dataset name for the `vst` table that
`ai3/src/com/kas/ai/kdb/load_vprof.q:9-22` loads every evening. One symbol, one date:

| `time` | `vmed` holds |
|---|---|
| 0 | **ADV**: median daily volume of the **continuous session only**, in shares |
| 1 | **open auction** volume |
| 2 | **close auction** volume |
| 3 | **PM reopen auction** volume (0 where there is no lunch break) |
| 4 … ~9 | other header rows, then zero rows before the open |
| ≥ 100 | a **minute of the day**; `vmed` is the **cumulative fraction of continuous volume**, 0 at the open rising to 1 at the close |

The key point: **the cumulative curve contains no auction volume.** The auctions are
only in the header rows, so a chart drawn from the minute rows alone has no open
and no close.

Example, `402340.KS` on 2026.11.17: ADV 595,283, open 8,301, close 49,331, PM 0;
zeros from 450 to 480, then 481 → 0.0181, 482 → 0.0315, 490 → 0.1059, … → 1.0.

Which date is requested: the profile dated D is built at D's end of day and is used
for trading on the next day, so the blotter asks for the **previous business day**,
and goes further back (up to 5 business days) when a day is empty or q returns an
error (`poll/backend/profile.py:264-336`). `cc0` is not requested: this gateway
rejects that column.

## 2. The transformation, step by step

| # | Step | ai3 `getsymprof` | liveOrdersPOC |
|---|---|---|---|
| 1 | Read the header volumes: ADV = `time=0`, open = `time=1`, close = `time=2` | `adv_regular: first vmed`, `open_regular: first 1_vmed`, `close_regular: first 2_vmed` (`:48`) | `decode` (`poll/backend/profile.py:83-122`), also `time=3` (PM) |
| 2 | Find where the minute rows start and keep the open point (cumulative = 0) | hard-coded `n: 10` (`:46`): drop 10 rows, prepend 0 | first row with `time ≥ 100`, then keep the last zero row before the first positive one (`_header_skip`, `poll/backend/profile.py:54-67`) |
| 3 | Day total `vtot` | `adv + open + close` (`:49`) | `adv + open + close + PM` |
| 4 | Lay a regular 10-minute grid over the session | `ftg each time` (`:50`) | `_grid` (`poll/backend/profile.py:125-128`), one grid per session half when there is a lunch break |
| 5 | Read the cumulative curve on the grid, linearly | `interp` (`ai3/src/com/kas/ai/kdb/idxprof.q:1`) | `numpy.interp` (same linear interpolation, same flat ends) |
| 6 | Volume per bucket = difference of consecutive points | `1_deltas` (`:51`) | `cum[i+1] − cum[i]` (`final_profile`, `poll/backend/profile.py:131-163`) |
| 7 | Rescale to the whole day | buckets `× adv / vtot`, auctions `/ vtot` (`:52`) | same |
| 8 | Lay the day out | OPEN at first−10 min, the buckets, CLOSE at last+10 min (`:53`) | OPEN `[open−10, open)`, buckets, PM `[reopen−10, reopen)`, CLOSE `[close, close+10)` |
| 9 | Running total | `volume_cum: sums volume_regular` | `cum`, ends at exactly 1.0 |

Result: one row per bar, `share` = fraction of the day's total volume, summing to 100%.

## 3. Deliberate differences from ai3

1. **PM auction included in `vtot`.** `getsymprof` sums only ADV + open + close, so on
   a lunch-break market (Tokyo, Shenzhen…) its shares total slightly over 100% and the
   PM auction is not shown. Here it is a bar at the lunch reopen and the day sums to 100%.
2. **Header row count read from the data, not `n: 10`.** `getsymprof` assumes exactly
   ten header/pre-open rows; Tokyo has one more (`7203.JP`: seven zero rows before the
   open), which shifts every bucket. The first `time ≥ 100` row is used instead.
3. **The grid ends on the real close.** `ftg` only survives in an OCR-damaged copy
   (`ai3/src/com/kas/ai/kdb/vproffb.q:665`); what is legible (first time, last time,
   600000 ms) and its use fix it as a 10-minute grid over the session. When a session
   is not a whole number of 10 minutes (7203.JP closes at 865), the last bucket is short
   (860–865) instead of the tail being dropped, so no volume is lost.
4. **The lunch break has no buckets.** The grid runs per session half; any volume the
   rows put inside the lunch gap lands in the first afternoon bucket.

## 4. Worked example: 7203.JP (2026.09.03 gateway rows)

ADV 17,110,800 · open 1,109,400 · close 5,570,500 · PM 247,700 → `vtot` = 24,038,400.

| Bar | Minutes | Share of the day | Running total |
|---|---|---|---|
| OPEN | 470–480 | 1,109,400 / 24,038,400 = **4.62%** | 4.62% |
| 08:00 | 480–490 | cum(490) = 0.130115 × 17,110,800 / 24,038,400 = **9.26%** | 13.88% |
| 08:10 | 490–500 | (0.207781 − 0.130115) × 0.7118 = **5.53%** | 19.41% |
| … | | | |
| PM | 680–690 | 247,700 / 24,038,400 = **1.03%** | 46.82% |
| 11:30 | 690–700 | **3.22%** | 50.04% |
| … | | | |
| 14:20 | 860–865 | **1.72%** (5-minute bucket) | 76.83% |
| CLOSE | 865–875 | 5,570,500 / 24,038,400 = **23.17%** | **100.00%** |

(The 08:00 bucket spans the uneven 481 / 482 / 490 rows; linear reading of the curve
at 490 gives 0.130115.)

## 5. On screen

- Cards: ADV (continuous), open auction, close auction, PM auction (when > 0), and the
  auctions' share of the day.
- Top panel: running total, share of the day (0 → 100%).
- Bottom panel: share of the day per bar; auctions in orange, labelled.
- Footer: the profile's date, the region clock, the source.
- Zoom (both tabs): once zoomed, drag the chart to move along the day (shift+drag
  still selects a range). Not zoomed, drag across either panel to zoom both on that
  time range, scroll
  the mouse wheel over the chart (zooms around the pointer; shift+wheel pans; fully
  zoomed out, wheel down scrolls the modal as usual), or pick
  a preset (Open, Morning, Afternoon, Close; those that fit the range). Zoomed, each
  panel's y-scale refits to what is in view: the bars to the tallest one in view, the
  running total to the visible range of its lines (the panel title then says the axis
  does not start at 0%). Double-click, Esc or "Reset zoom" zooms out; Esc closes the
  modal only when not zoomed. The zoom survives the 30s refresh of today's trades.

Code: `GET /api/profile?sym=` (`poll/backend/app.py:121`) → `build` / `final_profile`
(`poll/backend/profile.py:166`) → `FullDayTab` (`poll/frontend/profile.js:204`).
Cached per symbol and day.

## 6. How to check it by hand

1. Run the gateway call in section 1 for the previous business day.
2. ADV, open, close, PM = `vmed` at `time` 0, 1, 2, 3; `vtot` = their sum.
3. Open share = open / `vtot`; close share = close / `vtot`.
4. A bucket's share = (curve at its end − curve at its start) × ADV / `vtot`, reading the
   curve linearly between the minute rows.

For 402340.KS (2026.11.17): `vtot` = 652,915 → open **1.27%**, close **7.56%**,
continuous **91.17%**.

The same transformation is implemented in `experiment6/core-vp`
(`core/profile.py`, `final_profile`); the two give the same numbers.
