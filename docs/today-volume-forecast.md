# Today's traded volume and the forecast to the close

The **Full Day Volume Profile** tab also draws today's actual traded volume, bucketed
like the profile. While the session is still open, it adds a forecast for the rest of
the day in a second colour.

**Show** checkboxes switch each layer on or off:

- Volume profile
- Tilt (tag 148), shown only when there is a tilt
- Traded today
- Forecast to the close

| Where | What |
|---|---|
| kdb-queries | `queries/liquidity_profile/liquidity_profile.q` (`.lp.buckets`): the qatt read this follows |
| liveOrdersPOC | `QattSource`, `today_vs_profile`, `MockQatt` in `poll/backend/today.py`; `/api/today?sym=` (`&now=` to look at another moment); the chart in `poll/frontend/profile.js` (`FullDayTab`, `ProfileChart`) |
| Config | `QATT_HOST` / `QATT_PORT` (`QATT_USER` / `QATT_PASS`, falling back to `KDB_USER` / `KDB_PASS`): the QUOTES real-time server. Without them the trades are simulated. |

## The trades

`qatt` is on the **QUOTES** server, not the OMS one. Every row is a transaction, and
`price>0, size>0` is the whole test for a print. The real-time server holds today only
and has **no date column**, so nothing filters on a date. The query is
`liquidity_profile.q`'s, reduced to one row per minute on the server:

```q
{[sy] t:select time,size from qatt where sym=sy, price>0, size>0;
 0!select trades:count i, shares:sum "j"$size by bkt:"t"$60000*("j"$time) div 60000 from t}[`$"0700.HK"]
```

`qatt.time` is HKT, the same region clock as the VPROF profile and the orders, so each
minute falls into the profile's buckets with no conversion:

| Print time | Bucket |
|---|---|
| before the open | OPEN (the open auction prints before the session) |
| inside the lunch gap | PM (the afternoon reopen auction) |
| from the close | CLOSE (the closing auction) |
| otherwise | the 10-minute continuous bucket that holds it |

The minutes are cached for 10 s, and the tab refreshes every 30 s.

## The forecast: damped pace

- `E_i`: the volume the profile expects in bucket `i`, its share of the day × `vtot`.
- `E_day`: the whole day's expected volume (`vtot`).
- `E_now`: what the profile expected **by now**. Every finished bucket counts in full,
  and the current one counts for the part that has gone by. An auction counts once its
  bucket has ended or it has printed.
- `R`: what has traded today.

```
pace     = (R + 0.1·E_day) / (E_now + 0.1·E_day)
forecast = pace × E_i × (part of bucket i still to come)
```

Today's pace (`R / E_now`) is pulled towards 1 **as if a tenth of a normal day had
already traded exactly on profile**. A quiet or busy first ten minutes therefore barely
moves the forecast, and by mid-day it follows today's actual pace.

- **Example:** trading at twice the profile, the forecast pace is 1.5 at 10% of the
  day, 1.83 at half-day and 1.91 at the close.
- **"Pace vs profile" KPI:** shows the raw ratio `R / E_now`, with the damped value in
  its tooltip.
- **After the close:** once the close auction has printed there is nothing left to
  forecast. The forecast checkbox is then disabled, and the KPI reads "Day's volume".

## How it is drawn

- **Today's shares:** today's bars and line are a **share of today's total**, i.e.
  traded so far plus the forecast. Today's shape is then on the same 0–100% scale as
  the profile's even though the day's real total is not known yet. The tooltip gives
  the shares: traded, number of trades, the profile's expected volume, and the forecast.
- **Colours and line styles:**
  - traded: aqua, solid line
  - forecast: violet, dashed line, starting from "now"
  - profile: an outline (blue continuous, orange auctions) behind today's bars, and a
    black line
- **Palette check:** the four colours pass the dataviz palette validator in light and
  dark. In light mode aqua is 2.7:1 against the background, which is why the legend
  and tooltip carry the numbers and the forecast line is dashed.
- **With a tilt:** the tilted line and the market's dashed line are drawn as described
  in [the tilt page](profile-tilt-tag148.md). Today is always compared with the market
  profile, never the tilted one.

## Limits

- **Pace scales the profile's shape.** The forecast does not learn how today's shape
  differs from the profile, for example a heavier afternoon than usual.
- **Auctions are found by the clock.** A print stamped exactly on the open minute
  counts as continuous. kdb-queries' `.ms.probeSession` shows where a market's auction
  prints really fall.
- **The profile is the previous business day's**, as on the rest of the tab.
