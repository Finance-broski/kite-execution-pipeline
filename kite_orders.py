"""Target-book order placement for Indian equities on Zerodha Kite. SAFE BY DEFAULT.

    python kite_orders.py --target sample_target.csv
        -> PLAN mode (default): fetch holdings, compute deltas, write
           planned_orders_<ts>.csv. Places NOTHING.
    ... --live
        -> places orders. Requires today's token, a duplicate-order check, AND
           typing the confirmation phrase it prints. Refuses otherwise.

Design:
- Target CSV (symbol, weight, shares, approx_value_rs) is a TARGET BOOK, not a
  trade list. Orders = diff(target, current Kite CNC holdings). Initial deployment
  is the same diff against an empty book. Idempotent ACROSS sessions (unfilled DAY
  orders expire at EOD; re-run next session regenerates only the residue).
- LIMIT orders only, never MARKET; tick-snapped, marketable, inside a protection
  band. Unfilled residue is surfaced by kite_reconcile.py - a human decides.
- Slices any order above --max-slice-value rupees (default 75,000).
- Rate limit ~0.8 orders/sec - an order of magnitude under SEBI's 10/sec line.
"""
import argparse
import json
import math
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd

TICK = 0.05


# -- pure logic (unit-tested in test_kite_pipeline.py) -------------------------

def load_target(csv_path: str) -> dict:
    """CSV -> {symbol: {"qty": int, "ref": float}}. Validates hard."""
    df = pd.read_csv(csv_path)
    need = {"symbol", "shares", "approx_value_rs"}
    if not need.issubset(df.columns):
        raise ValueError(f"target CSV missing columns {need - set(df.columns)}")
    out = {}
    for r in df.itertuples():
        qty = int(r.shares)
        if qty <= 0:
            continue
        ref = float(r.approx_value_rs) / qty
        if not (0 < ref < 1_000_000):
            raise ValueError(f"{r.symbol}: insane implied price {ref}")
        if r.symbol in out:
            raise ValueError(f"duplicate symbol {r.symbol} in target CSV")
        out[str(r.symbol)] = {"qty": qty, "ref": ref}
    if not out:
        raise ValueError("target CSV produced zero positions")
    return out


def compute_deltas(target: dict, holdings: dict) -> list:
    """Diff target book vs current. Returns [{symbol,side,qty,ref}], sells first."""
    orders = []
    for sym, h in holdings.items():
        t_qty = target.get(sym, {}).get("qty", 0)
        if h["qty"] > t_qty:
            orders.append({"symbol": sym, "side": "SELL",
                           "qty": h["qty"] - t_qty, "ref": h["ref"]})
    for sym, t in target.items():
        h_qty = holdings.get(sym, {}).get("qty", 0)
        if t["qty"] > h_qty:
            orders.append({"symbol": sym, "side": "BUY",
                           "qty": t["qty"] - h_qty, "ref": t["ref"]})
    return orders


def limit_price(side: str, ref: float, band: float) -> float:
    """Marketable protective LIMIT, snapped to the tick.

    BUY  -> ref*(1+band) snapped DOWN into the band, but never below a marketable
            price (tick-ceil of ref), else the order can't fill.
    SELL -> mirror: ref*(1-band) snapped UP, but never above tick-floor(ref).

    For sub-tick bands on low-priced names the marketable clamp wins, so the
    effective band is ~1 tick - intended (a sub-tick band is meaningless)."""
    if side == "BUY":
        band_cap = round(math.floor(ref * (1.0 + band) / TICK) * TICK, 2)
        marketable = round(math.ceil(ref / TICK) * TICK, 2)
        return max(band_cap, marketable)
    band_floor = round(math.ceil(ref * (1.0 - band) / TICK) * TICK, 2)
    marketable = round(math.floor(ref / TICK) * TICK, 2)
    return min(band_floor, marketable)


def slice_qty(qty: int, ref: float, max_value: float) -> list:
    """Split qty so each slice's notional <= max_value. Last slice takes remainder.
    NOTE: if a single share's price exceeds max_value, slices are 1 share each and
    individually exceed the cap - the cap is best-effort, not a hard guarantee for
    very high-priced names."""
    per = max(1, int(max_value // ref))
    n_full, rem = divmod(qty, per)
    return [per] * n_full + ([rem] if rem else [])


def _run_id(ts: str) -> str:
    """Compact, day-unique id (base36 of the timestamp digits) for order tags, so the tag
    stays inside Kite's 20-char alphanumeric `tag` budget."""
    n, s = int(ts.replace("_", "")), ""
    while n:
        n, r = divmod(n, 36)
        s = "0123456789abcdefghijklmnopqrstuvwxyz"[r] + s
    return s or "0"


def slice_tag(run_id: str, i: int) -> str:
    """Deterministic, unique-per-slice Kite order tag (<=20 alphanumeric chars).

    The tag is the CRASH-RECOVERY key: it is assigned and journaled to disk BEFORE the order is
    placed, so if the place_order RESPONSE (the order_id) is lost to a disconnect/crash, the order
    still exists at the broker and can be found by matching the order book on this tag. order_id is
    only the fast in-session join key; tag is what survives a lost response."""
    return f"rb{run_id}{i:04d}"[:20]


def _journal(journal, rec: dict) -> None:
    """Append-only crash-safe record: the INTENT (with tag) is written before placement and the
    RESULT (with order_id) after, each flushed+fsync'd. A crash therefore loses at most the single
    in-flight slice -- and even that one already has its tag on disk to recover by."""
    if journal is None:
        return
    journal.write(json.dumps(rec) + "\n")
    journal.flush()
    try:
        os.fsync(journal.fileno())
    except (OSError, AttributeError, ValueError):             # e.g. StringIO in tests
        pass


def open_order_blockers(orderbook: list, symbols: set) -> set:
    """Symbols that already have a LIVE (non-terminal) order in today's book.
    Placing again would double up: holdings reflect fills, not still-open orders."""
    live = {"OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED", "PUT ORDER REQ RECEIVED",
            "MODIFY PENDING", "OPEN PENDING", "VALIDATION PENDING"}
    blocked = set()
    for o in orderbook:
        if o.get("tradingsymbol") in symbols and str(o.get("status", "")).upper() in live:
            blocked.add(o.get("tradingsymbol"))
    return blocked


# -- broker side --------------------------------------------------------------

def fetch_holdings(kite) -> dict:
    return {h["tradingsymbol"]: {"qty": int(h["quantity"]) + int(h.get("t1_quantity", 0)),
                                 "ref": float(h["last_price"])}
            for h in kite.holdings() if int(h["quantity"]) + int(h.get("t1_quantity", 0)) > 0}


def place_all(kite, orders: list, band: float, max_value: float,
              run_id: str = "", journal=None, sleeper=time.sleep) -> list:
    """Place sliced protective-LIMIT orders. Each slice gets a unique deterministic `tag` that is
    journaled BEFORE placement and sent on the order, so a lost response / crash never loses the
    order -- kite_reconcile recovers it from the order book by tag. order_id = in-session join key;
    tag = crash-recovery key."""
    placed, i = [], 0
    for o in orders:
        px = limit_price(o["side"], o["ref"], band)
        for q in slice_qty(o["qty"], o["ref"], max_value):
            tag = slice_tag(run_id, i)
            i += 1
            rec = {**o, "qty": q, "limit": px, "tag": tag, "order_id": "", "status": "PENDING"}
            _journal(journal, rec)                            # persist INTENT (+tag) before placing
            try:
                oid = kite.place_order(
                    variety="regular", exchange="NSE", tradingsymbol=o["symbol"],
                    transaction_type=o["side"], quantity=q, product="CNC",
                    order_type="LIMIT", price=px, validity="DAY", tag=tag)
                rec = {**rec, "order_id": oid, "status": "sent"}
            except Exception as e:                            # noqa: BLE001
                rec = {**rec, "order_id": "", "status": f"ERROR {type(e).__name__}: {e}"}
            _journal(journal, rec)                            # persist RESULT (order_id) after
            placed.append(rec)
            sleeper(1.25)
    return placed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--band", type=float, default=0.010,
                    help="protective limit band fraction, default 0.01")
    ap.add_argument("--max-slice-value", type=float, default=75_000.0)
    a = ap.parse_args()

    target = load_target(a.target)
    from core.config import get_settings
    from kite_auth import load_token_fresh
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=get_settings().kite_api_key)
    kite.set_access_token(load_token_fresh())

    try:
        holdings = fetch_holdings(kite)
    except Exception as e:                                    # noqa: BLE001
        sys.exit(f"could not read holdings from Kite: {type(e).__name__}: {e}")

    orders = compute_deltas(target, holdings)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id = _run_id(ts)
    here = os.path.dirname(os.path.abspath(__file__))
    total_slices = sum(len(slice_qty(o["qty"], o["ref"], a.max_slice_value)) for o in orders)

    plan = pd.DataFrame([{**o, "limit": limit_price(o["side"], o["ref"], a.band),
                          "slices": len(slice_qty(o["qty"], o["ref"], a.max_slice_value))}
                         for o in orders])
    plan_path = os.path.join(here, f"planned_orders_{ts}.csv")
    plan.to_csv(plan_path, index=False)
    n_sell = sum(1 for o in orders if o["side"] == "SELL")
    print(f"target {len(target)} names | held {len(holdings)} | orders {len(orders)} "
          f"({n_sell} sells first) | {total_slices} slices | plan -> {plan_path}")
    if plan.empty:
        print("book already matches target - nothing to do.")
        return
    if not a.live:
        print("PLAN mode (default). Review the CSV. Re-run with --live to place.")
        return

    try:
        live_book = kite.orders()
    except Exception as e:                                    # noqa: BLE001
        sys.exit(f"could not read order book to check for duplicates: {type(e).__name__}: {e}")
    blockers = open_order_blockers(live_book, {o["symbol"] for o in orders})
    if blockers:
        sys.exit(f"ABORT: {len(blockers)} symbol(s) already have live orders today "
                 f"({', '.join(sorted(blockers))}). Filled orders update holdings; OPEN "
                 "ones don't, so re-running now would double up. Reconcile/cancel first.")

    phrase = f"PLACE {len(orders)} ORDERS ({total_slices} SLICES)"
    if input(f'type exactly "{phrase}" to proceed: ').strip() != phrase:
        sys.exit("confirmation mismatch - nothing placed.")
    journal_path = os.path.join(here, f"placed_journal_{ts}.jsonl")
    try:
        with open(journal_path, "a", encoding="utf-8") as jf:
            placed = place_all(kite, orders, a.band, a.max_slice_value,
                               run_id=run_id, journal=jf)
    except Exception as e:                                    # noqa: BLE001
        sys.exit(f"placement aborted mid-run: {type(e).__name__}: {e}\n"
                 f"  every slice's tag is journaled in {journal_path} -- run "
                 f"kite_reconcile.py to recover any lost order_ids by tag before re-running.")
    out = os.path.join(here, f"placed_orders_{ts}.csv")
    pd.DataFrame(placed).to_csv(out, index=False)             # convenience snapshot (incl. tag)
    errs = [p for p in placed if p["status"].startswith("ERROR")]
    print(f"placed {len(placed) - len(errs)}/{len(placed)} slices -> {out}")
    print(f"crash-safe journal (tag-keyed) -> {journal_path}")
    if errs:
        print(f"!! {len(errs)} ERRORS - read {out}, then run kite_reconcile.py")
    print(f"next: python kite_reconcile.py --intended {out}   (recovers lost order_ids by tag)")


if __name__ == "__main__":
    main()
