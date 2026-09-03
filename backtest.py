#!/usr/bin/env python3
"""Replay historical bars through the SAME strategy, risk, and broker code
the live bot uses.

    py -3.11 backtest.py --file data/history.csv
    py -3.11 backtest.py --file data/history.csv --strategy mean_reversion
    py -3.11 backtest.py --file data/history.csv --compare

--compare runs every strategy plus buy-and-hold and prints a table.

READ THIS BEFORE TRUSTING A RESULT
-----------------------------------
A backtest is a measurement of how a rule performed on data you already have.
It is not a prediction. The ways it misleads you, in rough order of how often
they bite:

1. You tried many variants and kept the best one. With enough attempts,
   something always looks good by chance. Decide your rule BEFORE you test.
2. The sample is too short. Twenty trades tells you almost nothing.
3. Costs are underestimated. Slippage here is a flat estimate; real fills on
   thin names are worse.
4. Survivorship: today's tickers are the ones that survived.

The comparison that matters is against buy-and-hold, not against zero.
"""

import argparse
import logging
import os
import sys
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.brokers.simulated import SimulatedBroker  # noqa: E402
from bot.data.feeds import ReplayFeed, bars_from_csv  # noqa: E402
from bot.journal import Journal  # noqa: E402
from bot.models import Side  # noqa: E402
from bot.risk import RiskLimits, RiskManager  # noqa: E402
from bot.strategies.starters import REGISTRY  # noqa: E402


class SilentJournal(Journal):
    """Backtests generate thousands of rows; skip disk I/O."""

    def __init__(self):
        self.equity_curve: List[float] = []

    def record_fill(self, fill, reason=""):
        pass

    def record_equity(self, equity, cash, open_positions, day_trades_used):
        self.equity_curve.append(equity)


def round_trips(fills):
    """Pair BUYs with SELLs per symbol (FIFO) to get realised trade P&L."""
    open_lots: Dict[str, list] = {}
    trades = []
    for f in fills:
        if f.side == Side.BUY:
            open_lots.setdefault(f.symbol, []).append([f.qty, f.price])
        else:
            remaining = f.qty
            lots = open_lots.get(f.symbol, [])
            while remaining > 1e-9 and lots:
                lot = lots[0]
                used = min(remaining, lot[0])
                trades.append((f.symbol, (f.price - lot[1]) * used))
                lot[0] -= used
                remaining -= used
                if lot[0] <= 1e-9:
                    lots.pop(0)
    return trades


def max_drawdown(curve: List[float]) -> float:
    peak, worst = float("-inf"), 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, (v - peak) / peak)
    return worst


def buy_and_hold(bars_by_symbol, cash: float) -> float:
    """Equal-weight buy at the first bar, hold to the last. The bar to beat."""
    syms = [s for s, b in bars_by_symbol.items() if len(b) >= 2]
    if not syms:
        return cash
    per = cash / len(syms)
    total = 0.0
    for s in syms:
        bars = bars_by_symbol[s]
        total += per * (bars[-1].close / bars[0].close)
    return total


def run_one(name, bars_by_symbol, symbols, cash, slippage, limits):
    strategy = REGISTRY[name]()
    broker = SimulatedBroker(starting_cash=cash, slippage_bps=slippage,
                             allow_fractional=True)
    risk = RiskManager(limits)
    risk.limits.pdt_enforcement = False  # daily bars: PDT is not meaningful
    journal = SilentJournal()
    feed = ReplayFeed(bars_by_symbol, warmup=strategy.lookback)

    from bot.engine import Engine
    engine = Engine(broker=broker, feed=feed, strategy=strategy, risk=risk,
                    symbols=symbols, journal=journal, poll_seconds=0)

    steps = feed.steps_remaining
    for _ in range(steps):
        if feed.exhausted:
            break
        engine.tick()

    final = broker.get_account()
    trades = round_trips(broker.fills)
    wins = [p for _, p in trades if p > 0]
    losses = [p for _, p in trades if p <= 0]

    return {
        "strategy": name,
        "final_equity": final.equity,
        "return_pct": (final.equity - cash) / cash * 100,
        "trades": len(trades),
        "win_rate": (len(wins) / len(trades) * 100) if trades else 0.0,
        "avg_win": (sum(wins) / len(wins)) if wins else 0.0,
        "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
        "max_dd": max_drawdown(journal.equity_curve) * 100,
        "fills": len(broker.fills),
    }


def main():
    p = argparse.ArgumentParser(description="Backtest against historical bars")
    p.add_argument("--file", default="data/history.csv")
    p.add_argument("--strategy", choices=sorted(REGISTRY), default="sma_crossover")
    p.add_argument("--compare", action="store_true",
                   help="run every strategy and compare against buy-and-hold")
    p.add_argument("--cash", type=float, default=100.0)
    p.add_argument("--slippage-bps", type=float, default=5.0)
    p.add_argument("--max-position-pct", type=float, default=0.25)
    p.add_argument("--stop-loss-pct", type=float, default=0.03)
    p.add_argument("--take-profit-pct", type=float, default=0.06)
    args = p.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    # A multi-year replay would emit thousands of per-trade log lines.
    # The summary table below is the output that matters.
    for noisy in ("bot.risk", "bot.engine", "bot.brokers.simulated", "bot.data.feeds"):
        logging.getLogger(noisy).setLevel(logging.CRITICAL)

    if not os.path.exists(args.file):
        print(f"No such file: {args.file}\n"
              f"Fetch data first:  py -3.11 fetch_history.py --symbols SPY,QQQ",
              file=sys.stderr)
        return 1

    bars = bars_from_csv(args.file)
    if not bars:
        print(f"No usable bars in {args.file}", file=sys.stderr)
        return 1

    symbols = sorted(bars)
    n = max(len(b) for b in bars.values())
    start = min(b[0].timestamp for b in bars.values())
    end = max(b[-1].timestamp for b in bars.values())

    print(f"\nData    : {', '.join(symbols)}")
    print(f"Bars    : {n} per symbol")
    print(f"Period  : {start:%Y-%m-%d} to {end:%Y-%m-%d}")
    print(f"Capital : ${args.cash:,.2f}   slippage: {args.slippage_bps} bps")

    limits = RiskLimits(
        max_position_pct=args.max_position_pct,
        stop_loss_pct=args.stop_loss_pct,
        take_profit_pct=args.take_profit_pct,
        max_daily_loss_pct=1.0,  # don't halt a multi-year backtest on one day
    )

    names = sorted(REGISTRY) if args.compare else [args.strategy]
    results = [run_one(nm, bars, symbols, args.cash, args.slippage_bps, limits)
               for nm in names]

    bh = buy_and_hold(bars, args.cash)
    bh_pct = (bh - args.cash) / args.cash * 100

    print("\n" + "-" * 78)
    print(f"{'strategy':<18}{'final':>11}{'return':>10}{'trades':>8}"
          f"{'win%':>7}{'max dd':>9}")
    print("-" * 78)
    for r in results:
        print(f"{r['strategy']:<18}${r['final_equity']:>10,.2f}"
              f"{r['return_pct']:>9.2f}%{r['trades']:>8}"
              f"{r['win_rate']:>6.0f}%{r['max_dd']:>8.1f}%")
    print(f"{'buy & hold':<18}${bh:>10,.2f}{bh_pct:>9.2f}%{'-':>8}{'-':>7}{'-':>9}")
    print("-" * 78)

    best = max(results, key=lambda r: r["return_pct"])
    print()
    if best["trades"] < 30:
        print(f"! Only {best['trades']} round trips. Too few to distinguish skill "
              f"from luck -- get more data or a strategy that trades more.")
    if best["return_pct"] <= bh_pct:
        print(f"! No strategy beat buy-and-hold ({bh_pct:+.2f}%). On this data, "
              f"holding was better than trading.")
    else:
        print(f"* {best['strategy']} beat buy-and-hold by "
              f"{best['return_pct'] - bh_pct:.2f} points. Before believing it: "
              f"was this the first rule you tried, or the best of many?")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
