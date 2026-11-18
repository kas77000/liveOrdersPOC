# liveOrdersPOC — how the features work

These pages explain what each feature computes and how, step by step, next to the ai3
code it follows, so the numbers can be checked against the algo engine.

| Page | What it covers | ai3 counterpart |
|---|---|---|
| [Activated orders](activated-orders.md) | Which orders the blotter shows by default, and the State column | `kdb-queries` scripts: `select state:last state … where state=` `` `activated `` |
| [Full Day Volume Profile](full-day-volume-profile.md) | The symbol's whole-day profile, auctions included (first tab of the order modal) | `getsymprof` in `ai3/src/com/kas/ai/kdb/getVolumeProfileReport.q` |
| [Order Volume Profile](order-volume-profile.md) | The profile cut to the order's window, as a share of the order (second tab) | `CustomProfile.toMap` in `ai3/src/com/kas/ai/CustomProfile.java` (live algo) |

## Conventions used in every page

- **Paths.** `ai3/…` is relative to the `Projects` folder (`ai3/src/com/kas/ai/…`).
  liveOrdersPOC paths are relative to this repository. The `poll/` and `realtime/`
  folders hold the same backend and frontend code; the links point to `poll/`.
- **Clock.** Every time is a **minute of the day on the region clock** (HKT,
  UTC+8): the VPROF profile, `target.t_start` / `t_end` and the RDB all use it. A
  Korean or Japanese order therefore opens at 08:00 here, which is 09:00 local.
- **Shares.** "Share of the day" means a fraction of the day's *total* volume,
  auctions included (`vtot`). "Share of the order" means a fraction of the order's
  own schedule (sums to 100% over its window).
- **Numbers in the examples** are real gateway rows (`7203.JP`, `402340.KS`) run
  through the code; the unit tests (`poll/tests/test_profile.py`) use the same rows.
