import pandas as pd

from backend.fixmsg import parse_fixmsg, participation
from backend.sources import MockSource
from backend.transform import build_rows, flow_type_of, market_of


def test_participation_from_9012_subtags():
    tags = parse_fixmsg("9001=LINE;9002=12:15:00;9012=5=4^6=12^15=1")
    assert participation("LINE", tags) == {"min": 4.0, "max": 12.0, "target": None}


def test_participation_ignores_unrelated_subtag_that_contains_5():
    tags = parse_fixmsg("9001=VWAP;9012=92=A,5^6=8")
    assert participation("VWAP", tags) == {"min": None, "max": 8.0, "target": None}


def test_part_target_sets_min_and_max():
    tags = parse_fixmsg("9001=PART;9012=7=10")
    assert participation("PART", tags) == {"min": 10.0, "max": 10.0, "target": 10.0}


def test_fallback_to_beta_delta_when_no_subtags():
    assert participation("VWAP", {}, beta=15, delta=0)["max"] == 15
    assert participation("SHADOW", {}, beta=0, delta=3)["min"] == 3
    assert participation("DMA", {}, beta=15, delta=3) == {"min": None, "max": None, "target": None}


def test_market_from_sym_suffix():
    assert market_of("005930.KS") == "KR"
    assert market_of("BBCA.IJ") == "INDO"
    assert market_of("AAPL") == "US"
    assert market_of("XYZ.ZZ") == "ZZ"


def test_flow_type_from_basket():
    assert flow_type_of("SILK_KR01") == "Swap"
    assert flow_type_of("apSilk") == "Swap"
    assert flow_type_of("BASK_A") == "Agency"
    assert flow_type_of(None) == "Agency"


def test_build_rows_uses_latest_make_and_tolerates_missing_tables():
    key = dict(id_server=1, id_target=7)
    target = pd.DataFrame([dict(**key, sym="7203.JP", side="buy", size=1000, limit_price=0.0,
                                algo="VWAP", beta=0.0, delta=0.0, t_start=pd.Timedelta("01:30:00"),
                                t_end=pd.Timedelta("05:00:00"), trader="jdoe",
                                basket="SILK_JP", fixmsg="9001=VWAP;9012=6=12")])
    state = pd.DataFrame([dict(**key, make=100), dict(**key, make=250)])
    [row] = build_rows(target, state)
    assert row["pct_filled"] == 25.0
    assert row["limit"] is None            # 0 -> market order
    assert row["start"] == "01:30:00"
    assert row["flow_type"] == "Swap"
    assert row["account"] == "jdoe"
    assert row["client"] is None and row["amount_usd"] is None


def test_mock_source_round_trips():
    rows = build_rows(**MockSource().fetch())
    assert len(rows) == 30
    assert all(r["amount_usd"] for r in rows)
