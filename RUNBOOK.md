# Kite execution pipeline — runbook

Human-in-the-loop monthly job by design: Kite tokens die daily and the book
rebalances monthly, so a human is in the chain every time real orders can happen.
No daemon exists.

## One-time setup (~30 min)
1. developers.kite.trade → create a personal Connect app (FREE tier — we place
   orders only; market data stays on your existing pipeline). Redirect URL can be
   `http://127.0.0.1` (we only read the request_token off the address bar).
2. Whitelist your STATIC IP in the app settings (SEBI retail-algo framework; get a
   static IP from your ISP first if you don't have one).
3. `cp .env.example .env` and fill `KITE_API_KEY` / `KITE_API_SECRET`.
4. `pip install -r requirements.txt`, then `python test_kite_pipeline.py` (should be
   14/14, no broker needed).
5. Zero-capital plumbing test (account unfunded): run the monthly procedure below.
   Every order REJECTS on margin — that is the point: auth, placement, order-book
   read and reconciliation all get exercised with zero rupees at risk.

## Monthly rebalance procedure
    REM 1. token (browser login, ~1 min)
    python kite_auth.py
    python kite_auth.py --request-token XXXX
    python kite_auth.py --check                       REM confirms the token is live
    REM 2. produce a target book CSV (symbol, weight, shares, approx_value_rs).
    REM    sample_target.csv is provided as the format reference.
    REM 3. PLAN (places nothing) — READ the planned_orders csv
    python kite_orders.py --target sample_target.csv
    REM 4. PLACE (duplicate-order check + typed confirmation required)
    python kite_orders.py --target sample_target.csv --live
    REM 5. reconcile after ~15 min (exit code 0 = done)
    python kite_reconcile.py --intended placed_orders_<ts>.csv

## Frozen design decisions (change = logged amendment, not a quiet edit)
- Target-diff, not trade-list. Idempotent across sessions: unfilled DAY orders
  expire at EOD; re-running next session regenerates only the residue.
- LIMIT only, never MARKET. Marketable protective band, tick-snapped. Residue is
  reported, never chased intraday.
- Sells before buys (frees funds under CNC).
- Slices cap notional at ~Rs 75k/order; ~0.8 orders/sec (SEBI line is 10/sec).
- `--live` aborts if any targeted symbol already has a live order today.
- The execution layer computes NO performance; fills go to fills_log.csv.
