#!/usr/bin/env python3
"""Entry point.

    python run.py --mode sim                 # no credentials, no network
    python run.py --mode webull-paper        # Webull paper account
    python run.py --mode sim --ticks 200 --strategy mean_reversion

Create a file named HALT in this directory at any time to stop trading.
"""

import argparse
import logging
import os
import sys

from bot.brokers.simulated import SimulatedBroker
from bot.data.feeds import SyntheticFeed, WebullFeed
from bot.engine import Engine
from bot.journal import Journal
from bot.risk import RiskLimits, RiskManager
from bot.strategies.starters import REGISTRY


def load_env(path: str = ".env") -> None:
    """Minimal .env reader so secrets never live in the source tree."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Paper trading bot")
    p.add_argument(
        "--mode",
        choices=["sim", "live-data-sim", "webull-paper"],
        default="sim",
        help=(
            "sim: fake prices + fake broker (offline). "
            "live-data-sim: REAL Webull prices + local simulated broker (recommended). "
            "webull-paper: real prices + orders sent to a Webull account."
        ),
    )
    p.add_argument("--strategy", choices=sorted(REGISTRY), default="sma_crossover")
    p.add_argument("--symbols", default="SPY,QQQ,AAPL")
    p.add_argument("--cash", type=float, default=100.0, help="sim mode only")
    p.add_argument("--ticks", type=int, default=0, help="0 = run until stopped")
    p.add_argument("--poll", type=int, default=60, help="seconds between ticks")
    p.add_argument("--slippage-bps", type=float, default=5.0)
    p.add_argument("--max-position-pct", type=float, default=0.25)
    p.add_argument("--stop-loss-pct", type=float, default=0.03)
    p.add_argument("--take-profit-pct", type=float, default=0.06)
    p.add_argument("--max-daily-loss-pct", type=float, default=0.05)
    p.add_argument("--no-fractional", action="store_true")
    p.add_argument("--verbose", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    if not args.verbose:
        # The Webull SDK dumps the full signed request on every error, three
        # times over, which buries our own messages. Our adapters already log
        # a one-line summary of each failure. Use --verbose to see the raw dump.
        for noisy in ("webull", "webull.core", "webull.core.client"):
            lg = logging.getLogger(noisy)
            lg.setLevel(logging.CRITICAL)
            lg.propagate = False
            lg.handlers.clear()
    load_env()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    strategy = REGISTRY[args.strategy]()
    limits = RiskLimits(
        max_position_pct=args.max_position_pct,
        stop_loss_pct=args.stop_loss_pct,
        take_profit_pct=args.take_profit_pct,
        max_daily_loss_pct=args.max_daily_loss_pct,
    )
    risk = RiskManager(limits)

    if args.mode == "sim":
        broker = SimulatedBroker(
            starting_cash=args.cash,
            slippage_bps=args.slippage_bps,
            allow_fractional=not args.no_fractional,
        )
        feed = SyntheticFeed(symbols)
        poll = 0 if args.ticks else args.poll
    elif args.mode == "live-data-sim":
        # Real market data, locally simulated fills. No orders leave this
        # machine, so no brokerage account or paper funding is required.
        key = os.environ.get("WEBULL_APP_KEY")
        secret = os.environ.get("WEBULL_APP_SECRET")
        if not all([key, secret]):
            print(
                "Missing credentials. Copy .env.example to .env and fill in "
                "WEBULL_APP_KEY and WEBULL_APP_SECRET (account ID not needed "
                "for this mode).",
                file=sys.stderr,
            )
            return 1
        endpoint = os.environ.get("WEBULL_API_ENDPOINT", "api.sandbox.webull.com")
        region = os.environ.get("WEBULL_REGION", "us")
        broker = SimulatedBroker(
            starting_cash=args.cash,
            slippage_bps=args.slippage_bps,
            allow_fractional=not args.no_fractional,
        )
        try:
            feed = WebullFeed(key, secret, region=region, endpoint=endpoint)
        except ConnectionError as exc:
            print(f"\n{exc}\n", file=sys.stderr)
            return 1
        logging.getLogger(__name__).info(
            "live-data-sim: real Webull prices, simulated fills, $%.2f local cash",
            args.cash,
        )
        poll = args.poll
    else:
        key = os.environ.get("WEBULL_APP_KEY")
        secret = os.environ.get("WEBULL_APP_SECRET")
        account_id = os.environ.get("WEBULL_ACCOUNT_ID")
        if not all([key, secret, account_id]):
            print(
                "Missing credentials. Copy .env.example to .env and fill in:\n"
                "  WEBULL_APP_KEY, WEBULL_APP_SECRET, WEBULL_ACCOUNT_ID",
                file=sys.stderr,
            )
            return 1
        from bot.brokers.webull import WebullBroker
        # Paper/sandbox keys authenticate ONLY against the sandbox host.
        # Override with WEBULL_API_ENDPOINT in .env if Webull changes it.
        endpoint = os.environ.get("WEBULL_API_ENDPOINT", "api.sandbox.webull.com")
        region = os.environ.get("WEBULL_REGION", "us")
        try:
            broker = WebullBroker(key, secret, account_id, region=region, endpoint=endpoint)
        except ConnectionError as exc:
            print(f"\n{exc}\n", file=sys.stderr)
            return 1
        if not broker.check_connection():
            print(
                "\nCould not authenticate with Webull.\n"
                "  - Confirm WEBULL_API_ENDPOINT matches your key type\n"
                "    (paper keys -> api.sandbox.webull.com)\n"
                "  - Confirm the app key/secret are complete and current\n"
                "  - The account list above shows valid WEBULL_ACCOUNT_ID values\n",
                file=sys.stderr,
            )
            return 1
        feed = WebullFeed(key, secret, region=region, endpoint=endpoint)
        poll = args.poll

    engine = Engine(
        broker=broker, feed=feed, strategy=strategy, risk=risk,
        symbols=symbols, journal=Journal(),
        poll_seconds=poll, allow_fractional=not args.no_fractional,
    )
    engine.run(max_ticks=args.ticks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
