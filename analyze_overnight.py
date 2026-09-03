#!/usr/bin/env python3
"""Test the overnight-return anomaly on your own data.

    py -3.11 analyze_overnight.py --file data/history.csv

THE HYPOTHESIS
--------------
Split each day's return into two pieces:

    overnight = open[t]  / close[t-1] - 1     (while the market is closed)
    intraday  = close[t] / open[t]    - 1     (while the market is open)

Multiply them together and you get the daily return, so they account for
everything. The claim is that overnight has historically delivered most of the
long-run return while intraday delivered little or none.

WHY IT MIGHT BE REAL (a strategy needs a reason, not just a backtest)
    - Overnight holders carry gap risk they cannot hedge or exit; the
      premium may be compensation for that.
    - Funds systematically trade near the close, pushing prices in ways that
      reverse by the next open.
    - Retail flow concentrates during the day and is on average uninformed.

WHY IT MIGHT NOT BE TRADEABLE
    - Capturing it needs two trades EVERY day. Costs compound brutally.
    - The open and close are the least liquid, widest-spread moments.
    - It is widely published, so any easy version is likely arbitraged.

This script measures the effect AND the costs. Both matter.
"""

import argparse
import math
import os
import statistics
import sys
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.data.feeds import bars_from_csv  # noqa: E402
from bot.models import Bar  # noqa: E402


def decompose(bars: List[Bar]):
    """Return (overnight, intraday, full_day) simple-return series."""
    overnight, intraday, full = [], [], []
    for prev, cur in zip(bars, bars[1:]):
        if prev.close <= 0 or cur.open <= 0:
            continue
        overnight.append(cur.open / prev.close - 1)
        intraday.append(cur.close / cur.open - 1)
        full.append(cur.close / prev.close - 1)
    return overnight, intraday, full


def compound(returns, start=100.0, cost_per_leg=0.0):
    """Grow `start` through the series, charging cost on each entry+exit."""
    v = start
    for r in returns:
        v *= (1 + r - 2 * cost_per_leg)
    return v


def stats(returns):
    n = len(returns)
    if n < 2:
        return {}
    mean = statistics.fmean(returns)
    sd = statistics.stdev(returns)
    t = mean / (sd / math.sqrt(n)) if sd else 0.0
    return {
        "n": n,
        "mean_bps": mean * 10_000,
        "sd_bps": sd * 10_000,
        "t_stat": t,
        "annualised": (1 + mean) ** 252 - 1,
        "sharpe": (mean / sd * math.sqrt(252)) if sd else 0.0,
        "win_rate": sum(1 for r in returns if r > 0) / n * 100,
    }


def show_symbol(sym: str, bars: List[Bar], capital: float):
    overnight, intraday, full = decompose(bars)
    if len(overnight) < 30:
        print(f"\n{sym}: not enough bars ({len(overnight)})")
        return None

    print("\n" + "=" * 74)
    print(f"{sym}   {bars[0].timestamp:%Y-%m-%d} to {bars[-1].timestamp:%Y-%m-%d}"
          f"   ({len(overnight)} sessions)")
    print("=" * 74)

    rows = [("overnight", overnight), ("intraday", intraday), ("full day", full)]
    print(f"{'segment':<12}{'mean(bps)':>11}{'ann.ret':>10}{'sharpe':>9}"
          f"{'win%':>7}{'t-stat':>9}")
    print("-" * 74)
    for label, series in rows:
        s = stats(series)
        print(f"{label:<12}{s['mean_bps']:>11.2f}{s['annualised']*100:>9.1f}%"
              f"{s['sharpe']:>9.2f}{s['win_rate']:>6.0f}%{s['t_stat']:>9.2f}")

    print("\nGrowth of ${:,.0f}, no trading costs:".format(capital))
    print(f"  buy & hold (full day)  ${compound(full, capital):>12,.2f}")
    print(f"  overnight only         ${compound(overnight, capital):>12,.2f}")
    print(f"  intraday only          ${compound(intraday, capital):>12,.2f}")

    print("\nOvernight strategy after costs (2 trades per day):")
    print(f"  {'cost/leg':<12}{'final':>14}{'vs buy&hold':>14}")
    bh = compound(full, capital)
    for bps in (0, 1, 2, 5, 10):
        v = compound(overnight, capital, cost_per_leg=bps / 10_000)
        print(f"  {str(bps) + ' bps':<12}${v:>13,.2f}{v - bh:>+14,.2f}")

    return {"symbol": sym, "overnight": stats(overnight),
            "intraday": stats(intraday), "full": stats(full),
            "bh": bh, "on_0": compound(overnight, capital),
            "on_5": compound(overnight, capital, cost_per_leg=0.0005)}


def main():
    p = argparse.ArgumentParser(description="Overnight vs intraday decomposition")
    p.add_argument("--file", default="data/history.csv")
    p.add_argument("--capital", type=float, default=100.0)
    args = p.parse_args()

    if not os.path.exists(args.file):
        print(f"No such file: {args.file}\n"
              f"Run:  py -3.11 fetch_history.py --symbols SPY,QQQ --timespan D",
              file=sys.stderr)
        return 1

    bars_by_symbol: Dict[str, List[Bar]] = bars_from_csv(args.file)
    if not bars_by_symbol:
        print(f"No usable bars in {args.file}", file=sys.stderr)
        return 1

    results = []
    for sym in sorted(bars_by_symbol):
        r = show_symbol(sym, bars_by_symbol[sym], args.capital)
        if r:
            results.append(r)

    print("\n" + "=" * 74)
    print("HOW TO READ THIS")
    print("=" * 74)
    print("""
t-stat  : roughly how many standard errors the mean is from zero. Above ~2 is
          the conventional threshold for "probably not noise". Below that, you
          cannot distinguish the effect from randomness with this sample.
sharpe  : return per unit of volatility, annualised. Buy-and-hold on an index
          is typically 0.4-0.6. Anything above 1.5 from a simple rule should
          make you suspect a bug, not celebrate.
cost/leg: a realistic retail round trip on a liquid ETF is 1-5 bps per leg.
          On anything less liquid, assume worse.
""")

    for r in results:
        sym, on = r["symbol"], r["overnight"]
        beats_free = r["on_0"] > r["bh"]
        beats_real = r["on_5"] > r["bh"]
        strong = abs(on["t_stat"]) > 2

        print(f"{sym}:")
        if not strong:
            print(f"  Overnight t-stat is {on['t_stat']:.2f} -- too weak to call "
                  f"an effect on this sample.")
        else:
            print(f"  Overnight t-stat is {on['t_stat']:.2f} -- statistically "
                  f"distinguishable from zero.")
        if beats_free and not beats_real:
            print("  Beats buy-and-hold with zero costs, LOSES at realistic "
                  "costs. This is the usual outcome, and it is why cost "
                  "modelling is not optional.")
        elif beats_real:
            print("  Still beats buy-and-hold at 5 bps/leg. Worth a second "
                  "look -- check the cost assumption before believing it.")
        else:
            print("  Does not beat buy-and-hold even with zero costs on this "
                  "sample.")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
