# Profile tilt — tag 148 (front / back loading)

The **Order Volume Profile** tab has a second row of inputs, **Tilt · tag 148**
(`alpha`, `beta`, `mode`). They replay what the algo engine does with FIX 9012
sub-tag 148, so you can see how an order's schedule bends away from the plain volume
profile. The inputs start from the order's own tag 148 when it has one (read from
`target.fixmsg`) and can be edited; **Reset to order** puts them back.

| Where | What |
|---|---|
| ai3 parsing | `Parameters.__p_148` — `ai3/src/com/kas/ai/Parameters.java:2637`: `148=alpha,beta[,mode]` |
| ai3 engine | `ProfileSnapshot` — `ai3/src/com/kas/ai/ProfileSnapshot.java` (transcribed from the photos in `ai3/profileSnapshot/`): constructor `:150-157`, `setVolumeFrac` `:588-802`, `tilt` `:488` |
| liveOrdersPOC | `parse_tag148`, `tilt_order_profile`, `_tilt` in `poll/backend/profile.py`; `/api/order_profile?…&alpha=&beta=&mode=`; the inputs in `poll/frontend/profile.js` (`OrderTab`) |

## The parameters

- **alpha**: the share of the order that is moved. `alpha > 0` **front-loads**,
  `alpha < 0` **back-loads**.
- **beta**: the part of the window's *trading time* the moved share goes into. That is
  the first `beta` when front-loading, the last `beta` when back-loading.
- **mode** (optional, default 0):
  - `0` tilts the continuous volume only;
  - `1` also re-weights the auctions;
  - `2` scales alpha by the profile's progress at the breakpoint;
  - `3` does both `1` and `2`.

The engine applies a tilt only when `|alpha| > 1e-5` and `0 < beta < 1`
(`ProfileSnapshot.java:157`). Otherwise the page says "inactive" and draws the plain profile.

## How the engine computes it

The engine keeps a target for "volume done so far", `v1t`, out of the window's
volume `v12`. Both are in market volume and both come from the volume profile. The
tilt adds an amount to `v1t`.

1. **Breakpoint** (`:648-656`). Let `t12` be the window's trading minutes without the
   lunch break (`deltaTime`, `:221`). The breakpoint is at
   `itb = tend − (1 − beta)·t12` (front) or `tend − beta·t12` (back). If the market has
   a lunch break and `itb ≤ lunch end`, it is moved back by the lunch length
   (`:657-658`), so that the minutes are counted without lunch.
2. **Volume at the breakpoint** (`:653, :666`). While the order is before the
   breakpoint (`t1t ≤ t12·beta`, or `·(1−beta)` when back-loading), `v1b` = the
   profile's expected volume from the start to `itb`. After that it is frozen.
3. **Tilt** (`tilt`, `:488`). Let `x = v1t / v12` (progress through the window's
   volume) and `b = v1b / v12` (progress at the breakpoint).
   - **Front-load** (alpha > 0): target = `(1 − α)·x + α·min(1, x / b)`. The α share
     is spread over the volume before the breakpoint and is complete when it is reached.
   - **Back-load** (alpha < 0): remaining = `(1 − |α|)·(1 − x) + |α|·min(1, (1 − x)/(1 − b))`.
     Nothing of the |α| share is done before the breakpoint; all of it is done after.
4. **Auctions** (`:670-713`, modes 1 and 3). The open, PM and close volumes join `v12`.
   - Each auction becomes `auction·(1 − |α|) + (the auction if it is on the tilted side)·|α|·v12 / (vb + tilted auctions)`.
   - With a front-load the open gains and the close loses; with a back-load it is the
     reverse. The PM auction counts on the tilted side if it falls there.
5. **Mode 2 / 3** (`:697`): `alpha ← alpha · v1b / v12` before all of the above.

liveOrdersPOC runs these formulas at the end of each 10-minute bucket of the order's
window, with the profile's volumes in place of the live ones. It draws:

- the **tilted** running total (solid line), with the bars as the tilted share per bucket;
- the **untilted** running total (dashed line, "Without the tilt");
- the **breakpoint** (dashed orange line) and its KPI: the time, and `b`, the share of
  the window's continuous volume the profile reaches by then.

## On the Full Day Volume Profile tab (a what-if)

The **Full Day Volume Profile** tab has the same inputs, filled in from the order's
tag 148. There the tilt is applied **as if an order ran from the open to the close,
every auction included** (`tilt_full_day` in `poll/backend/profile.py`;
`/api/profile?sym=…&alpha=&beta=&mode=`).

- Such an order's schedule is the day profile itself: its share of the order equals
  the share of the day. So the same engine formulas apply unchanged.
- The breakpoint is at `beta` of the whole session's trading time.
- **The engine never computes this.** It only tilts real orders over their own window.
  The tab shows how a tag 148 value bends a whole day, with the market's profile as
  the dashed line.
- The KPIs (ADV, auction volumes) stay the market's own numbers.
- With alpha or beta empty, the tab shows the plain `getsymprof` profile, as before.

## Example (`7203.JP`, 08:25 → 13:10, real gateway rows, `poll/tests/test_profile.py`)

The window has 225 trading minutes (lunch 10:30–11:30 is excluded). With
`148=0.5,0.3`, the breakpoint is 30% × 225 = 67.5 minutes after the start: **09:32**.
The profile has `b` = 39.8% of the window's volume by then.

| Time | Plain profile | `0.5,0.3` (front) | `-0.5,0.3` (back) |
|---|---|---|---|
| 09:00 | 22.6% | 39.7% = 0.5·22.6% + 0.5·(22.6/39.8) | 11.3% |
| 10:00 | 49.0% | 73.3% | 24.5% |
| 11:30 | 60.9% | 80.5% | 31.7% |
| 12:00 | 76.9% | 88.5% | 39.7% |
| 13:10 | 100% | 100% | 100% |

## Engine behaviour to know about

- **Breakpoint before the start.** The lunch shift in step 1 applies whenever
  `itb ≤ lunch end`. For a window that ends **before or during lunch**, this moves the
  breakpoint before the order starts. For example, 10:00 → 11:30 HK with `0.5,0.5`
  puts it at 09:15.
  - `b` then follows `x`, so the front-loaded α share is due at once.
  - At the very first snapshot `b = 0`, which makes `x/b` 0/0 = NaN.
  - The page replays this and notes it under the chart. Whether it is intended is a
    question for the algo team.
- **Auction weights change at the breakpoint.** Once past it, the engine resets `itb`
  to 0 (`:644`). With mode 1 / 3 the PM auction then stops counting on the tilted side,
  so the running total can step a little. The page notes when this happens.
- **What is not replayed:**
  - the moo / moc fill adjustments (`setVolumeFrac(moo, moopm, moc)`);
  - TMX scaling, the `vsc` / `tsc` close scaling;
  - halts.
- **What is assumed:**
  - **`v1t`, `t1t`, `t12` meaning.** `NewLogModel.profileSnapshot`, which feeds
    `v1t`, `t1t` and `t12`, is not in ai3. They are taken as the profile's expected
    volume and trading minutes since the order's start. The window is clipped to the
    session for the minutes.
  - **Transcription.** `ProfileSnapshot.java` was transcribed from photos. The tilt
    path (`:488`, `:588-802`) was read carefully, but check the rest against the class
    before relying on it.
