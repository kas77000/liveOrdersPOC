"""backend.profile: the whole-day volume profile, auctions included (as getsymprof).
Rows are the same fixtures core-vp uses: a Shenzhen and a real Tokyo gateway result."""
import datetime as dt

import pandas as pd
import pytest

from backend.profile import (MockVprof, ProfileError, VprofSource, build, decode,
                             final_profile, previous_business_day, profile_query)

HEAD = [(0, 105156029.0), (1, 445700.0), (2, 799800.0), (3, 2000.0)]
PRE = [(540, 0.0), (550, 0.0), (560, 0.0), (565, 0.0), (568, 0.0), (569, 0.0)]
OBSERVED = [(570, 0.0), (571, 0.0216089), (572, 0.0380905), (580, 0.134472),
            (590, 0.222817), (600, 0.285873), (610, 0.341493), (620, 0.389694),
            (630, 0.428962), (640, 0.463602)]


def _tail():
    """650..690 then 780..900, rising smoothly from 0.463602 to 1.0."""
    mins = list(range(650, 700, 10)) + list(range(780, 910, 10))
    start, end = 0.463602, 1.0
    n = len(mins)
    return [(m, start + (end - start) * (i + 1) / n) for i, m in enumerate(mins)]


JP_HEAD = [(0, 17110800.0), (1, 1109400.0), (2, 5570500.0), (3, 247700.0)]
JP_PRE = [(450, 0.0), (460, 0.0), (470, 0.0), (475, 0.0), (478, 0.0),
          (479, 0.0), (480, 0.0)]
JP_BODY = [
    (481, 0.022515), (482, 0.038998), (490, 0.130115), (500, 0.207781),
    (510, 0.260936), (520, 0.300778), (530, 0.334088), (540, 0.367217),
    (550, 0.39882), (560, 0.428228), (570, 0.456119), (580, 0.482308),
    (590, 0.503572), (600, 0.522482), (610, 0.541178), (620, 0.559821),
    (630, 0.57841),
    (690, 0.57841),                      # lunch: 60 minutes, no volume
    (691, 0.586857), (692, 0.592787), (700, 0.623645), (710, 0.649343),
    (720, 0.67235), (730, 0.692777), (740, 0.712701), (750, 0.732652),
    (760, 0.751223), (770, 0.770456), (780, 0.789536), (790, 0.808187),
    (800, 0.828522), (810, 0.848276), (820, 0.868308), (830, 0.890806),
    (840, 0.91493), (850, 0.942629), (859, 0.972427), (860, 0.975855),
    (861, 0.979696), (862, 0.984017), (863, 0.988698), (864, 0.994013),
    (865, 1.0),
]




def _raw(head, body):
    rows = head + body
    return pd.DataFrame({"time": [t for t, _ in rows], "vmed": [v for _, v in rows]})


SZ = decode(_raw(HEAD, PRE + OBSERVED + _tail()))
JP = decode(_raw(JP_HEAD, JP_PRE + JP_BODY))


@pytest.fixture(params=["shenzhen", "tokyo"])
def p(request):
    return {"shenzhen": SZ, "tokyo": JP}[request.param]


def test_the_day_sums_to_one_with_open_first_and_close_last(p):
    rows = final_profile(p)
    assert sum(r["share"] for r in rows) == pytest.approx(1.0)
    assert rows[-1]["cum"] == pytest.approx(1.0)
    assert rows[0]["label"] == "OPEN" and rows[-1]["label"] == "CLOSE"
    assert rows[0]["share"] == pytest.approx(p["v_open"] / p["vtot"])
    assert rows[-1]["share"] == pytest.approx(p["v_close"] / p["vtot"])


def test_continuous_buckets_carry_adv_over_vtot(p):
    cont = [r for r in final_profile(p) if r["phase"] == "continuous"]
    assert sum(r["share"] for r in cont) == pytest.approx(p["adv"] / p["vtot"])


def test_tokyo_grid_lunch_and_pm_auction():
    assert JP["lunch"] == (630, 690) and JP["open_min"] == 480 and JP["close_min"] == 865
    rows = final_profile(JP)
    cont = [r for r in rows if r["phase"] == "continuous"]
    widths = [r["end"] - r["start"] for r in cont]
    assert widths[:-1] == [10] * (len(widths) - 1) and widths[-1] == 5
    assert not any(630 <= r["start"] < 690 for r in cont)
    pm = [r for r in rows if r["phase"] == "pm auction"][0]
    assert (pm["start"], pm["end"]) == (680, 690)
    # 480-490 spans the uneven 481/482/490 knots: linear interp gives cum(490)
    assert cont[0]["share"] == pytest.approx(0.130115 * JP["adv"] / JP["vtot"])


def test_cc0_is_used_when_present():
    raw = _raw(JP_HEAD, JP_PRE + JP_BODY)
    raw["cc0"] = [float(len(JP_HEAD) + len(JP_PRE) - 1)] + [0.0] * (len(raw) - 1)
    assert decode(raw)["minutes"] == JP["minutes"]


def test_bad_rows_raise_a_readable_error():
    with pytest.raises(ProfileError, match="header"):
        decode(_raw([(0, 1.0)], JP_BODY))
    with pytest.raises(ProfileError, match="no profile rows"):
        decode(pd.DataFrame(columns=["time", "vmed"]))


def test_gateway_call_is_one_string_without_cc0():
    """The OMS VPROF gateway rejects cc0; this is the call checked by hand."""
    assert profile_query("402340.KS", dt.date(2026, 11, 17)) == (
        "get_data_by_date[`profile;`date`sym`vmed`time;2026.11.17;2026.11.17;`402340.KS]")


class QError(Exception):
    """Stands in for pykx.exceptions.QError (matched by class name)."""


class Fake(VprofSource):
    def __init__(self, answers):
        super().__init__("fake", 0)
        self.answers, self.asked = answers, []

    def _rows(self, sym, date):
        self.asked.append(date)
        a = self.answers.get(date, pd.DataFrame())
        if isinstance(a, Exception):
            raise a
        return a


JP_RAW = _raw(JP_HEAD, JP_PRE + JP_BODY)


def test_reads_the_previous_business_day_first():
    src = Fake({dt.date(2026, 11, 17): JP_RAW})
    out = src.get("7203.JP", today=dt.date(2026, 11, 18))
    assert src.asked == [dt.date(2026, 11, 17)]
    assert out["date"] == "2026-11-17"
    src.get("7203.JP", today=dt.date(2026, 11, 18))
    assert len(src.asked) == 1                                  # cached


def test_monday_reads_friday_and_skips_empty_or_failing_days():
    src = Fake({dt.date(2026, 11, 13): QError("holiday"),     # Friday: a q error
                dt.date(2026, 11, 11): JP_RAW})                 # 12th: empty
    out = src.get("7203.JP", today=dt.date(2026, 11, 16))      # a Monday
    assert src.asked == [dt.date(2026, 11, 13), dt.date(2026, 11, 12),
                         dt.date(2026, 11, 11)]
    assert out["date"] == "2026-11-11"


def test_a_connection_failure_stops_at_once():
    src = Fake({dt.date(2026, 11, 17): ConnectionRefusedError("down")})
    with pytest.raises(ConnectionRefusedError):
        src.get("7203.JP", today=dt.date(2026, 11, 18))
    assert len(src.asked) == 1


def test_nothing_in_the_lookback_says_so_with_the_q_errors():
    src = Fake({dt.date(2026, 11, 17): QError("type")})
    with pytest.raises(ProfileError, match="5 business days.*type"):
        src.get("7203.JP", today=dt.date(2026, 11, 18))
    assert len(src.asked) == 5


def test_a_korean_profile_like_the_gateway_returned_decodes():
    """402340.KS on 2026.11.17: header and first rows as read off the gateway (tail
    extended to the close). No lunch break, PM auction 0."""
    head = [(0, 595283.0), (1, 8301.0), (2, 49331.0), (3, 0.0)]
    pre = [(450, 0.0), (460, 0.0), (470, 0.0), (475, 0.0), (478, 0.0), (479, 0.0),
           (480, 0.0)]
    seen = [(481, 0.0181334), (482, 0.0314832), (490, 0.10586), (500, 0.169977),
            (510, 0.214949), (520, 0.254571), (530, 0.292069)]
    tail = [(m, 0.292069 + (1 - 0.292069) * (k + 1) / 38)
            for k, m in enumerate(range(540, 920, 10))]
    out = build(_raw(head, pre + seen + tail), "402340.KS", dt.date(2026, 11, 17))
    assert out["lunch"] is None and out["open_min"] == 480
    rows = out["rows"]
    assert [rows[0]["label"], rows[-1]["label"]] == ["OPEN", "CLOSE"]
    assert not any(r["phase"] == "pm auction" for r in rows)
    assert sum(r["share"] for r in rows) == pytest.approx(1.0)
    assert rows[0]["share"] == pytest.approx(8301 / (595283 + 8301 + 49331))


def test_mock_profile_is_a_valid_day():
    out = MockVprof().get("7203.JP", today=dt.date(2026, 11, 18))
    assert sum(r["share"] for r in out["rows"]) == pytest.approx(1.0)
    assert out == MockVprof().get("7203.JP", today=dt.date(2026, 11, 18))   # stable


@pytest.mark.parametrize("bad", ["", "a b", "x];exit 0;[", "`x", "7203.JP;hclose 0"])
def test_anything_but_a_ticker_never_reaches_q(bad):
    with pytest.raises(ProfileError, match="not a symbol"):
        profile_query(bad, dt.date(2026, 11, 18))
