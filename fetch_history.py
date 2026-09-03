#!/usr/bin/env python3
"""Download historical bars from Webull into a CSV for backtesting.

    py -3.11 fetch_history.py --symbols SPY,QQQ --timespan D --bars 1200
    py -3.11 fetch_history.py --symbols SPY --timespan M5 --bars 3000

Webull caps a single request at 1200 bars, so this pages backwards using
end_time when you ask for more. Output goes to data/history.csv by default.

Timespans: M1 M5 M15 M30 M60 D W M
Daily bars give the most history per request -- 1200 daily bars is ~5 years,
which is far more useful for judging a strategy than a few days of minutes.
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.data.feeds import WebullFeed, bars_from_csv, bars_to_csv  # noqa: E402
from run import load_env  # noqa: E402

log = logging.getLogger("fetch")
MAX_PER_REQUEST = 1200


def fetch_symbol(feed, symbol, timespan, total_wanted):
    """Page backwards until we have enough bars or the API stops giving more."""
    collected = {}
    end_time = None
    while len(collected) < total_wanted:
        want = min(MAX_PER_REQUEST, total_wanted - len(collected))
        try:
            res = feed._data.market_data.get_history_bar(
                symbol, feed.category, timespan,
                count=str(want), end_time=end_time,
            )
            if res.status_code != 200:
                log.error("%s: HTTP %s %s", symbol, res.status_code, res.text[:200])
                break
            batch = WebullFeed._parse(symbol, res.json())
        except Exception:
            log.exception("%s: request failed", symbol)
            break

        if not batch:
            break

        before = len(collected)
        for b in batch:
            collected[b.timestamp] = b
        gained = len(collected) - before
        log.info("%s: +%d bars (%d total, oldest %s)",
                 symbol, gained, len(collected),
                 min(collected).strftime("%Y-%m-%d"))

        if gained == 0:
            break  # API is repeating itself; no more history available
        end_time = int(min(collected).timestamp() * 1000) - 1

    return [collected[k] for k in sorted(collected)]


def main():
    p = argparse.ArgumentParser(description="Download historical bars")
    p.add_argument("--symbols", default="SPY,QQQ")
    p.add_argument("--timespan", default="D",
                   choices=["M1", "M5", "M15", "M30", "M60", "D", "W", "M"])
    p.add_argument("--bars", type=int, default=1200,
                   help="bars per symbol (paged automatically above 1200)")
    p.add_argument("--out", default="data/history.csv")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_env()
    for noisy in ("webull", "webull.core", "webull.core.client"):
        lg = logging.getLogger(noisy)
        lg.setLevel(logging.CRITICAL)
        lg.propagate = False
        lg.handlers.clear()

    key = os.environ.get("WEBULL_APP_KEY")
    secret = os.environ.get("WEBULL_APP_SECRET")
    if not key or not secret:
        print("Missing WEBULL_APP_KEY / WEBULL_APP_SECRET in .env", file=sys.stderr)
        return 1

    endpoint = os.environ.get("WEBULL_API_ENDPOINT", "api.sandbox.webull.com")
    region = os.environ.get("WEBULL_REGION", "us")

    try:
        feed = WebullFeed(key, secret, region=region, endpoint=endpoint)
    except ConnectionError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    all_bars = {}
    for sym in symbols:
        bars = fetch_symbol(feed, sym, args.timespan, args.bars)
        if bars:
            all_bars[sym] = bars
            log.info("%s: %d bars  %s -> %s", sym, len(bars),
                     bars[0].timestamp.strftime("%Y-%m-%d"),
                     bars[-1].timestamp.strftime("%Y-%m-%d"))
        else:
            log.warning("%s: no bars returned", sym)

    if not all_bars:
        print("Nothing fetched.", file=sys.stderr)
        return 1

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    n = bars_to_csv(args.out, all_bars)
    print(f"\nWrote {n} bars to {args.out}")
    print(f"Now run:  py -3.11 backtest.py --file {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
