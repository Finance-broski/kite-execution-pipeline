"""Kite Connect daily auth - human-in-the-loop, by design.

Kite access tokens EXPIRE DAILY (~6am IST). For a monthly-rebalance book this is
a feature: a human logs in on rebalance morning, runs the pipeline, and the
credential dies by itself. No daemon, no token-refresh machinery.

Usage (from the repo root, venv active):
    python kite_auth.py                      -> prints login URL
    python kite_auth.py --request-token XXXX -> exchanges + saves today's token
    python kite_auth.py --check              -> validates today's token (authoritative)

Token file: %KITE_TOKEN_PATH% or C:\\trading-secrets\\kite_token.json (gitignored).
API creds come from core.config Settings (KITE_API_KEY / KITE_API_SECRET via .env).
Compliance: the Kite app must have your STATIC IP whitelisted (SEBI retail-algo
framework). The stored "date" is the save date; --check is the authoritative
freshness test (it calls kite.profile()).
"""
import argparse
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TOKEN_PATH = os.environ.get("KITE_TOKEN_PATH", r"C:\trading-secrets\kite_token.json")


def _settings():
    from core.config import get_settings
    s = get_settings()
    if not s.kite_api_key or not s.kite_api_secret:
        sys.exit("KITE_API_KEY / KITE_API_SECRET missing - copy .env.example to .env "
                 "and fill from your Kite Connect app at developers.kite.trade.")
    return s


def save_token(access_token: str, user_id: str) -> None:
    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        json.dump({"access_token": access_token, "date": date.today().isoformat(),
                   "user_id": user_id}, f)
    try:
        os.chmod(TOKEN_PATH, 0o600)          # best-effort: owner-only
    except OSError:
        pass
    print(f"token saved -> {TOKEN_PATH} (valid today only)")


def load_token_fresh() -> str:
    """Return today's access token or raise RuntimeError. Calendar-date guard only;
    --check is the authoritative validity test."""
    if not os.path.exists(TOKEN_PATH):
        raise RuntimeError(f"no token file at {TOKEN_PATH} - run kite_auth.py first")
    with open(TOKEN_PATH, encoding="utf-8") as f:
        tok = json.load(f)
    if tok.get("date") != date.today().isoformat():
        raise RuntimeError(f"token is from {tok.get('date')}, not today - Kite tokens "
                           "expire daily. Re-run kite_auth.py (browser login).")
    if not tok.get("access_token"):
        raise RuntimeError("token file malformed (no access_token)")
    return tok["access_token"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--request-token", default=None)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    s = _settings()
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=s.kite_api_key)

    if a.check:
        kite.set_access_token(load_token_fresh())
        prof = kite.profile()
        print(f"token OK - {prof.get('user_id')} / {prof.get('user_name')}")
        return

    if not a.request_token:
        print("1. Open this URL, log in, approve:")
        print(f"   {kite.login_url()}")
        print("2. You land on your redirect URL with ?request_token=XXXX in it.")
        print("3. Re-run:  python kite_auth.py --request-token XXXX")
        return

    try:
        sess = kite.generate_session(a.request_token, api_secret=s.kite_api_secret)
    except Exception as e:                                   # noqa: BLE001
        sys.exit(f"session exchange failed: {type(e).__name__}: {e}")
    save_token(sess["access_token"], sess.get("user_id", "?"))


if __name__ == "__main__":
    main()
