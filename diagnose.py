#!/usr/bin/env python3
"""Print RAW Webull API responses so we can see actual field names.

    py -3.11 diagnose.py

Reads credentials from .env. Prints the unmodified JSON from each endpoint --
no parsing, no guessing. Use this whenever a value reads as 0.00 or empty:
it shows whether the data is missing or just under a key we didn't expect.

Safe to share the output: it prints API responses, not your key or secret.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run import load_env  # noqa: E402


def show(label, res):
    print("\n" + "=" * 70)
    print(label)
    print("=" * 70)
    if res is None:
        print("  (call raised an exception -- see traceback above)")
        return
    print(f"HTTP {res.status_code}")
    try:
        print(json.dumps(res.json(), indent=2)[:3000])
    except Exception:
        print(res.text[:2000])


def main():
    load_env()
    key = os.environ.get("WEBULL_APP_KEY")
    secret = os.environ.get("WEBULL_APP_SECRET")
    account_id = os.environ.get("WEBULL_ACCOUNT_ID", "")
    endpoint = os.environ.get("WEBULL_API_ENDPOINT", "api.sandbox.webull.com")
    region = os.environ.get("WEBULL_REGION", "us")

    if not key or not secret:
        print("Missing WEBULL_APP_KEY / WEBULL_APP_SECRET in .env", file=sys.stderr)
        return 1

    from webull.core.client import ApiClient
    from webull.data.data_client import DataClient
    from webull.trade.trade_client import TradeClient

    print(f"Endpoint : {endpoint}")
    print(f"Region   : {region}")
    print(f"Account  : {account_id or '(not set)'}")

    client = ApiClient(key, secret, region)
    client.add_endpoint(region, endpoint)

    trade = TradeClient(client)

    def try_call(label, fn):
        try:
            show(label, fn())
        except Exception as exc:
            print("\n" + "=" * 70)
            print(label)
            print("=" * 70)
            print(f"  FAILED: {type(exc).__name__}: {str(exc)[:500]}")

    try_call("1. ACCOUNT LIST", lambda: trade.account_v2.get_account_list())

    if account_id:
        try_call("2. ACCOUNT BALANCE (field names matter here)",
                 lambda: trade.account_v2.get_account_balance(account_id))
        try_call("3. ACCOUNT POSITIONS",
                 lambda: trade.account_v2.get_account_position(account_id))
    else:
        print("\n(skipping balance/positions -- WEBULL_ACCOUNT_ID not set)")

    data = DataClient(client)
    try_call("4. HISTORY BARS for SPY (1-minute, 5 bars)",
             lambda: data.market_data.get_history_bar("SPY", "US_STOCK", "M1", count="5"))

    print("\n" + "=" * 70)
    print("Done. Share this output to get the parsers fixed.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
