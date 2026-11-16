import pandas as pd

from backend.sources import COLUMNS, AGG, INCREMENTAL_Q, KdbSource, OrderCache
from backend.transform import build_rows


def _target(i, **kw):
    row = dict(id_server=1, id_target=i, sym="7203.JP", side="buy", size=100, limit_price=0.0,
               algo="VWAP", beta=0.0, delta=0.0, t_start=pd.Timedelta(0), t_end=pd.Timedelta(0),
               trader="jdoe", basket="B", fixmsg="")
    row.update(kw)
    return row


class FakeRdb(KdbSource):
    """KdbSource whose RDB is a dict of in-memory, append-only lists."""

    def __init__(self):
        super().__init__("fake", 0)
        self.tables = {t: [] for t in COLUMNS}
        self.reads = []

    def _read_new(self, table, n):
        rows = self.tables[table]
        self.reads.append((table, n))
        if len(rows) < n:
            return True, len(rows), pd.DataFrame(rows, columns=COLUMNS[table])
        return False, len(rows), pd.DataFrame(rows[n:], columns=COLUMNS[table])


def test_incremental_q_takes_new_rows_collapsed_per_order():
    q = INCREMENTAL_Q.format(t="target_state", agg=AGG["target_state"],
                             cols="id_server,id_target,make")
    assert q == ("{[n] c:count target_state; r:c<n; "
                 "(r; c; 0!select max make by id_server,id_target from "
                 "select id_server,id_target,make from target_state where i>=$[r;0;n])}")
    assert AGG["target"] == "0!select"     # last row per order, like OrderCache


def test_poll_reads_only_new_rows():
    rdb = FakeRdb()
    rdb.tables["target"] += [_target(1), _target(2)]
    rdb.tables["target_state"] += [dict(id_server=1, id_target=1, make=10)]
    rdb.fetch()
    rdb.tables["target_state"] += [dict(id_server=1, id_target=1, make=30),
                                   dict(id_server=1, id_target=2, make=5)]
    frames = rdb.fetch()
    assert ("target_state", 1) in rdb.reads and ("target", 2) in rdb.reads
    st = frames["target_state"].set_index("id_target")["make"]
    assert st.to_dict() == {1: 30, 2: 5}
    assert len(frames["target"]) == 2


def test_poll_reloads_after_rdb_reset():
    rdb = FakeRdb()
    rdb.tables["target"] += [_target(1), _target(2), _target(3)]
    rdb.fetch()
    rdb.tables["target"] = [_target(9)]             # end of day: RDB emptied, new order
    frames = rdb.fetch()
    assert frames["target"]["id_target"].tolist() == [9]
    assert rdb.seen["target"] == 1


def test_missing_optional_table_degrades():
    class NoClient(FakeRdb):
        def _read_new(self, table, n):
            if table == "target_client":
                raise RuntimeError("target_client")
            return super()._read_new(table, n)
    rdb = NoClient()
    rdb.tables["target"] += [_target(1)]
    [row] = build_rows(**rdb.fetch())
    assert row["client"] is None


def test_cache_make_never_goes_backwards():
    c = OrderCache()
    c.apply("target_state", pd.DataFrame([dict(id_server=1, id_target=1, make=50)]))
    c.apply("target_state", pd.DataFrame([dict(id_server=1, id_target=1, make=20)]))  # stale replay
    assert c.frames()["target_state"]["make"].tolist() == [50]
