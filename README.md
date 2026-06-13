# Kite Execution Pipeline — SEBI-aware, human-in-the-loop

> **For algo vendors & brokers getting empanelment-ready:** this is the kind of
> SEBI-compliant execution architecture I implement as a service — static-IP
> routing, marketable protective limits, Algo-ID/audit logging, and the
> empanelment doc pack. Free gap audit → **AyanJain259@gmail.com**.

A small, safe-by-default order-execution layer for Indian equities on
[Kite Connect](https://kite.trade/), built to sit inside the **personal-use lane**
of SEBI's post-April-2026 retail-algo framework: static-IP whitelisting, daily
OAuth tokens, LIMIT-only orders, and a hard rate limit an order of magnitude
under the 10-orders/sec line. A human is in the chain every time real orders can
happen — there is no daemon.

> **Scope note:** this proves *personal-use* compliance (the &le;10 OPS / static-IP /
> human-in-the-loop lane). It is **not** a commercial vendor product — turning a
> paid algo product into an *empanelled* one (broker-server routing, ISO 27001 +
> CERT-In VAPT, Algo-ID tagging, RA registration for black-box) is the separate
> service layer I build for vendors and brokers.

## Run it (clone → test in ~2 minutes)
```bash
pip install -r requirements.txt
cp .env.example .env          # then fill KITE_API_KEY / KITE_API_SECRET
python test_kite_pipeline.py  # 14 pure-logic tests, no broker/token/network needed
```

## Design decisions worth stealing
- **Target-diff, not trade-list:** orders = `diff(target portfolio, live holdings)`.
  Initial deployment, rebalances, and partial-fill recovery are ONE idempotent path.
- **Marketable protective LIMITs, never MARKET:** buy snapped down into `ref*(1+band)`
  but never below a fillable price; sell mirrored. Tick-snapped, quote-API-free.
- **Sells before buys** (frees CNC funds), notional slicing, ~0.8 orders/sec.
- **Human-in-the-loop by construction:** Kite tokens expire daily; the pipeline is a
  monthly morning routine, not a daemon. The credential dies by itself every night.
- **Duplicate-order guard:** `--live` refuses to place if a symbol already has a live
  order today (open orders don't show up in holdings — re-running would double up).
- **Reconciliation with teeth:** fills vs intent, side-signed slippage, append-only
  log, exit code 0 only when every order is COMPLETE. The machine never chases residue.

## Files
`kite_auth.py` (daily token, human login) → `kite_orders.py` (PLAN by default;
`--live` needs a duplicate check + typed confirmation) → `kite_reconcile.py`
(fills vs intent → `fills_log.csv`). `test_kite_pipeline.py` has 14 mocked-broker
tests. `RUNBOOK.md` is the monthly procedure. `core/config.py` loads creds from `.env`.

## Security
Token and API secret live in `.env` / a local token file, both gitignored; the
token file is written owner-only and expires daily. This is the personal tier — a
production vendor deployment uses managed secrets, which is part of the service.
