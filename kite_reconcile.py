"""Post-execution reconciliation - fills vs intent, to an append-only log.

    python kite_reconcile.py --intended placed_orders_X.csv

Pulls today's order book from Kite, joins against the placed-orders CSV, and:
1. prints a per-order verdict (COMPLETE / PARTIAL x/y / OPEN / REJECTED),
2. appends rows to fills_log.csv (the permanent record),
3. exits 0 only if every intended order is COMPLETE - the exit code itself tells
   you whether the rebalance is finished.

slippage_cost_pct is side-signed: positive = adverse to you (paid above ref on a
buy, or sold below ref on a sell). The machine never chases residue; a human
re-runs kite_orders.py next session and the target-diff regenerates what's missing.
"""
import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd

FILLS_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fills_log.csv")


def classify(intended_qty: int, ob_row: dict | None) -> tuple:
    """-> (verdict, filled_qty, avg_price). Pure; unit-tested."""
    if ob_row is None:
        return "MISSING_FROM_ORDERBOOK", 0, 0.0
    status = str(ob_row.get("status", "")).upper()
    filled = int(ob_row.get("filled_quantity", 0) or 0)
    avg = float(ob_row.get("average_price", 0) or 0)
    if status == "COMPLETE" and filled >= intended_qty:
        return "COMPLETE", filled, avg
    if status in ("REJECTED", "CANCELLED"):
        return status, filled, avg
    if filled > 0:
        return f"PARTIAL {filled}/{intended_qty}", filled, avg
    return "OPEN", 0, 0.0


def reconcile(intended: pd.DataFrame, orderbook: list) -> pd.DataFrame:
    """Join placed orders vs the Kite order book. `order_id` is the fast in-session join key; when
    it is missing (e.g. the place_order response was lost to a crash/disconnect), fall back to the
    pre-assigned `tag` to RECOVER the order from the book. Pure; unit-tested."""
    by_id = {str(o.get("order_id")): o for o in orderbook}
    by_tag = {str(o.get("tag")): o for o in orderbook if o.get("tag")}
    rows = []
    for r in intended.itertuples():
        oid = str(r.order_id) if str(r.order_id) not in ("", "nan") else None
        tag = str(getattr(r, "tag", "")) if str(getattr(r, "tag", "")) not in ("", "nan") else None
        ob_row = by_id.get(oid) if oid else None
        recovered = False
        if ob_row is None and tag and tag in by_tag:          # crash recovery: response was lost,
            ob_row = by_tag[tag]                              # find the order by its pre-set tag
            oid = str(ob_row.get("order_id") or "") or oid
            recovered = True
        verdict, filled, avg = classify(int(r.qty), ob_row)
        ref = float(r.ref)
        cost = 0.0
        if avg and ref:
            raw = (avg - ref) / ref * 100.0
            cost = round(raw if str(r.side).upper() == "BUY" else -raw, 3)  # +ve adverse
        rows.append({"ts": datetime.now().isoformat(timespec="seconds"),
                     "symbol": r.symbol, "side": r.side, "intended_qty": int(r.qty),
                     "limit": float(r.limit), "order_id": oid or "", "tag": tag or "",
                     "recovered_by_tag": recovered,
                     "verdict": verdict, "filled_qty": filled, "avg_price": avg,
                     "slippage_cost_pct": cost})
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--intended", required=True, help="placed_orders_*.csv from kite_orders")
    a = ap.parse_args()
    intended = pd.read_csv(a.intended)
    if intended.empty:
        sys.exit("intended CSV is empty - nothing to reconcile")

    from core.config import get_settings
    from kite_auth import load_token_fresh
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=get_settings().kite_api_key)
    kite.set_access_token(load_token_fresh())
    try:
        book = kite.orders()
    except Exception as e:                                    # noqa: BLE001
        sys.exit(f"could not read order book from Kite: {type(e).__name__}: {e}")

    rep = reconcile(intended, book)
    hdr = not os.path.exists(FILLS_LOG)
    rep.to_csv(FILLS_LOG, mode="a", header=hdr, index=False)

    print(rep[["symbol", "side", "intended_qty", "verdict", "filled_qty",
               "avg_price", "slippage_cost_pct"]].to_string(index=False))
    bad = rep[rep["verdict"] != "COMPLETE"]
    print(f"\n{len(rep) - len(bad)}/{len(rep)} COMPLETE - appended to {FILLS_LOG}")
    if len(bad):
        print(f"!! {len(bad)} not complete. Policy: do NOT chase. "
              "Re-run kite_orders.py next session; the diff regenerates residue.")
        sys.exit(1)


if __name__ == "__main__":
    main()
