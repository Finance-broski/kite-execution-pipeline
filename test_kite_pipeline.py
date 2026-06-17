"""Unit tests for the pipeline's pure logic. No network, no broker, no token.

    python test_kite_pipeline.py        (standalone)
    python -m pytest -q                  (via pytest)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kite_orders import (TICK, compute_deltas, limit_price, load_target,
                         open_order_blockers, place_all, slice_qty)
from kite_reconcile import classify, reconcile


def test_load_target_parses_and_validates():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "t.csv")
        with open(p, "w") as f:
            f.write("symbol,weight,shares,approx_value_rs\n"
                    "IDEA,0.015,405,6054.75\nSBC,0.015,163,6047.3\nZERO,0.0,0,0\n")
        t = load_target(p)
    assert set(t) == {"IDEA", "SBC"}
    assert t["IDEA"]["qty"] == 405
    assert abs(t["IDEA"]["ref"] - 6054.75 / 405) < 1e-9


def test_load_target_rejects_duplicates():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "t.csv")
        with open(p, "w") as f:
            f.write("symbol,weight,shares,approx_value_rs\nA,0.5,10,100\nA,0.5,10,100\n")
        try:
            load_target(p)
            raise AssertionError("should have raised on duplicate")
        except ValueError:
            pass


def test_deltas_initial_deployment_is_all_buys():
    t = {"A": {"qty": 10, "ref": 100.0}, "B": {"qty": 5, "ref": 50.0}}
    o = compute_deltas(t, {})
    assert sorted((x["symbol"], x["side"], x["qty"]) for x in o) == [
        ("A", "BUY", 10), ("B", "BUY", 5)]


def test_deltas_rebalance_sells_first_and_diffs():
    t = {"A": {"qty": 10, "ref": 100.0}, "C": {"qty": 7, "ref": 70.0}}
    h = {"A": {"qty": 4, "ref": 99.0}, "B": {"qty": 8, "ref": 80.0},
         "C": {"qty": 7, "ref": 71.0}}
    o = compute_deltas(t, h)
    assert o[0]["side"] == "SELL" and o[0]["symbol"] == "B" and o[0]["qty"] == 8
    assert ("A", "BUY", 6) in [(x["symbol"], x["side"], x["qty"]) for x in o]
    assert all(x["symbol"] != "C" for x in o)
    assert len(o) == 2


def test_deltas_trim_position_sells_excess():
    t = {"A": {"qty": 3, "ref": 100.0}}
    o = compute_deltas(t, {"A": {"qty": 10, "ref": 101.0}})
    assert o == [{"symbol": "A", "side": "SELL", "qty": 7, "ref": 101.0}]


def test_limit_price_buy_floors_into_band():
    px = limit_price("BUY", 100.0, 0.01)
    assert px == 101.0
    px = limit_price("BUY", 99.99, 0.01)
    assert px == 100.95 and px <= 99.99 * 1.01


def test_limit_price_sell_ceils_into_band():
    px = limit_price("SELL", 99.99, 0.01)
    assert px == 99.0 and px >= 99.99 * 0.99
    assert round(px / TICK, 6) == round(px / TICK)


def test_limit_price_low_price_stays_marketable():
    # sub-tick band on a low-priced name must NOT land below ref (would never fill)
    bx = limit_price("BUY", 10.03, 0.001)
    assert bx >= 10.03 and abs(round(bx / TICK) - bx / TICK) < 1e-9
    sx = limit_price("SELL", 10.03, 0.001)
    assert sx <= 10.03 and abs(round(sx / TICK) - sx / TICK) < 1e-9
    # normal cases unchanged
    assert limit_price("BUY", 100.0, 0.01) == 101.0
    assert limit_price("SELL", 100.0, 0.01) == 99.0


def test_slice_respects_max_value():
    assert slice_qty(100, 100.0, 5000.0) == [50, 50]
    assert slice_qty(101, 100.0, 5000.0) == [50, 50, 1]
    assert slice_qty(7, 100.0, 5000.0) == [7]
    assert slice_qty(3, 90_000.0, 75_000.0) == [1, 1, 1]
    for q, ref, mv in [(100, 100.0, 5000.0), (101, 100.0, 5000.0), (3, 90000.0, 75000.0)]:
        assert sum(slice_qty(q, ref, mv)) == q


def test_open_order_blockers_flags_live_dupes():
    ob = [{"tradingsymbol": "A", "status": "OPEN"},
          {"tradingsymbol": "B", "status": "COMPLETE"},
          {"tradingsymbol": "C", "status": "TRIGGER PENDING"}]
    assert open_order_blockers(ob, {"A", "B", "C", "D"}) == {"A", "C"}


class FakeKite:
    def __init__(self, fail_on=None):
        self.calls, self.fail_on = [], fail_on or set()
    def place_order(self, **kw):
        self.calls.append(kw)
        if kw["tradingsymbol"] in self.fail_on:
            raise RuntimeError("InputException: insufficient funds")
        return f"OID{len(self.calls)}"


def test_place_all_slices_rate_limits_and_survives_errors():
    fk = FakeKite(fail_on={"BAD"})
    sleeps = []
    orders = [{"symbol": "OK", "side": "BUY", "qty": 100, "ref": 100.0},
              {"symbol": "BAD", "side": "BUY", "qty": 10, "ref": 50.0}]
    placed = place_all(fk, orders, band=0.01, max_value=5000.0,
                       sleeper=lambda s: sleeps.append(s))
    assert len(placed) == 3
    assert len(sleeps) == 3 and all(s >= 1.0 for s in sleeps)
    assert placed[0]["status"] == "sent" and placed[0]["order_id"] == "OID1"
    assert placed[2]["status"].startswith("ERROR")
    assert all(c["product"] == "CNC" and c["order_type"] == "LIMIT"
               and c["validity"] == "DAY" and c["exchange"] == "NSE"
               for c in fk.calls)


def test_classify_verdicts():
    assert classify(10, {"status": "COMPLETE", "filled_quantity": 10,
                         "average_price": 101.0}) == ("COMPLETE", 10, 101.0)
    assert classify(10, {"status": "OPEN", "filled_quantity": 4,
                         "average_price": 100.5})[0] == "PARTIAL 4/10"
    assert classify(10, {"status": "REJECTED", "filled_quantity": 0,
                         "average_price": 0})[0] == "REJECTED"
    assert classify(10, None)[0] == "MISSING_FROM_ORDERBOOK"
    assert classify(10, {"status": "OPEN", "filled_quantity": 0,
                         "average_price": 0})[0] == "OPEN"


def test_reconcile_joins_on_order_id():
    import pandas as pd
    intended = pd.DataFrame([
        {"symbol": "A", "side": "BUY", "qty": 10, "limit": 101.0,
         "ref": 100.0, "order_id": "X1", "status": "sent"},
        {"symbol": "B", "side": "SELL", "qty": 5, "limit": 99.0,
         "ref": 100.0, "order_id": "X2", "status": "sent"}])
    ob = [{"order_id": "X1", "status": "COMPLETE", "filled_quantity": 10,
           "average_price": 100.8},
          {"order_id": "X2", "status": "OPEN", "filled_quantity": 0,
           "average_price": 0}]
    rep = reconcile(intended, ob)
    assert list(rep["verdict"]) == ["COMPLETE", "OPEN"]
    assert abs(rep.iloc[0]["slippage_cost_pct"] - 0.8) < 1e-6


def test_reconcile_slippage_is_side_signed():
    import pandas as pd
    intended = pd.DataFrame([
        {"symbol": "A", "side": "BUY", "qty": 10, "limit": 101.0,
         "ref": 100.0, "order_id": "X1", "status": "sent"},
        {"symbol": "B", "side": "SELL", "qty": 5, "limit": 99.0,
         "ref": 100.0, "order_id": "X2", "status": "sent"}])
    ob = [{"order_id": "X1", "status": "COMPLETE", "filled_quantity": 10,
           "average_price": 100.8},   # paid 0.8% above ref -> +0.8 adverse
          {"order_id": "X2", "status": "COMPLETE", "filled_quantity": 5,
           "average_price": 99.0}]    # sold 1% below ref -> +1.0 adverse
    rep = reconcile(intended, ob)
    assert abs(rep.iloc[0]["slippage_cost_pct"] - 0.8) < 1e-6
    assert abs(rep.iloc[1]["slippage_cost_pct"] - 1.0) < 1e-6


def test_place_all_tags_unique_and_journaled():
    import io
    import json as _json
    fk = FakeKite()
    jf = io.StringIO()
    orders = [{"symbol": "OK", "side": "BUY", "qty": 100, "ref": 100.0}]    # -> 2 slices @ 5000 cap
    placed = place_all(fk, orders, band=0.01, max_value=5000.0, run_id="z9",
                       journal=jf, sleeper=lambda s: None)
    tags = [p["tag"] for p in placed]
    assert len(tags) == len(set(tags)) and all(len(t) <= 20 for t in tags)  # unique, <=20 chars
    assert all(c.get("tag") == p["tag"] for c, p in zip(fk.calls, placed))  # tag IS sent on the order
    lines = [_json.loads(x) for x in jf.getvalue().splitlines()]
    assert len(lines) == 2 * len(placed)                                    # intent + result per slice
    assert lines[0]["status"] == "PENDING" and lines[0]["tag"] == tags[0]   # INTENT journaled pre-place
    assert lines[1]["status"] == "sent" and lines[1]["order_id"]            # RESULT journaled after


def test_reconcile_recovers_by_tag_when_order_id_lost():
    import pandas as pd
    # the place_order RESPONSE was lost (crash): order_id empty, but tag was journaled pre-placement
    intended = pd.DataFrame([
        {"symbol": "A", "side": "BUY", "qty": 10, "limit": 101.0,
         "ref": 100.0, "order_id": "", "tag": "rbz90000", "status": "PENDING"}])
    ob = [{"order_id": "RECOVERED1", "tag": "rbz90000", "status": "COMPLETE",
           "filled_quantity": 10, "average_price": 100.5}]
    rep = reconcile(intended, ob)
    assert rep.iloc[0]["verdict"] == "COMPLETE"
    assert rep.iloc[0]["order_id"] == "RECOVERED1"          # recovered from the book by tag
    assert bool(rep.iloc[0]["recovered_by_tag"]) is True


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"  PASS {fn.__name__}")
    print(f"\n{len(fns)}/{len(fns)} tests pass")
