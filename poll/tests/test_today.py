"""backend.today: today's prints per profile bucket and the damped-pace forecast."""
import datetime as dt

import pandas as pd
import pytest

from backend.profile import MockVprof, ProfileError
from backend.today import MockQatt, minutes_from_frame, qatt_query, today_vs_profile

FULL = MockVprof().get("DEMO.KS", today=dt.date(2026, 11, 19))   # 09:30-11:30 | 13:00-16:00


def rows_by(t, phase):
    return [r for r in t["rows"] if r["phase"] == phase]


def test_the_query_is_liquidity_profiles_and_refuses_anything_but_a_ticker():
    q = qatt_query("0700.HK")
    assert "from qatt where sym=sy, price>0, size>0" in q and '[`$"0700.HK"]' in q
    with pytest.raises(ProfileError):
        qatt_query('x"];hclose 0;["')


def test_minutes_come_back_from_kdb_times():
    df = pd.DataFrame({"bkt": pd.to_timedelta(["09:31:00", "09:30:00"]), "trades": [2, 3],
                       "shares": [200, 300]})
    assert minutes_from_frame(df) == [(570, 3, 300), (571, 2, 200)]


def test_prints_fall_into_the_auction_and_continuous_buckets_by_the_clock():
    mins = [(560, 1, 1000), (575, 4, 40), (700, 1, 7), (778, 1, 500), (968, 1, 9000)]
    t = today_vs_profile(FULL, mins, now=990)
    assert rows_by(t, "open auction")[0]["actual"] == 1000
    assert rows_by(t, "pm auction")[0]["actual"] == 500 + 7   # the lunch gap
    assert rows_by(t, "close auction")[0]["actual"] == 9000
    first = rows_by(t, "continuous")[0]
    assert (first["start"], first["actual"]) == (570, 40)
    assert t["traded"] == 1000 + 40 + 7 + 500 + 9000


def test_once_the_close_has_printed_nothing_is_forecast():
    t = today_vs_profile(FULL, [(968, 1, 9000)], now=975)
    assert t["finished"] and t["forecast_rest"] == 0 and t["forecast_day"] == t["traded"]


def test_before_anything_trades_the_forecast_is_the_profile():
    t = today_vs_profile(FULL, [], now=500)
    assert t["pace"] == pytest.approx(1.0)
    assert t["forecast_day"] == pytest.approx(t["expected_day"])


def test_the_pace_is_damped_towards_1_by_a_tenth_of_a_day():
    full = FULL
    # trade exactly twice the profile up to 10:30
    mins = []
    for r in full["rows"]:
        if r["phase"] == "open auction":
            mins.append((r["start"], 1, int(2 * r["share"] * full["vtot"])))
        elif r["phase"] == "continuous" and r["end"] <= 630:
            mins.append((r["start"], 1, int(2 * r["share"] * full["vtot"])))
    t = today_vs_profile(full, mins, now=630)
    e_now, e_day = t["expected_now"], t["expected_day"]
    assert t["pace_raw"] == pytest.approx(2.0, rel=1e-3)
    assert t["pace"] == pytest.approx((2 * e_now + 0.1 * e_day) / (e_now + 0.1 * e_day), rel=1e-3)
    assert 1.0 < t["pace"] < 2.0
    # every bucket still to come is the profile at that pace; the past ones get nothing
    for r in t["rows"]:
        if r["end"] <= 630 and r["phase"] != "pm auction":
            assert r["forecast"] == 0
        if r["start"] >= 630:
            assert r["forecast"] == pytest.approx(t["pace"] * r["expected"])


def test_the_current_bucket_forecasts_only_what_is_left_of_it():
    t = today_vs_profile(FULL, [(570, 1, 100)], now=575)        # half way through 09:30
    first = rows_by(t, "continuous")[0]
    assert first["elapsed"] == pytest.approx(0.5)
    assert first["forecast"] == pytest.approx(t["pace"] * first["expected"] * 0.5)


def test_the_simulated_trades_stop_at_now():
    mins = MockQatt().get("DEMO.KS", FULL, now=640)
    assert mins and max(m for m, _, _ in mins) < 640
