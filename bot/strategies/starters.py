"""Strategies emit opinions, never orders. Sizing and safety live in risk.py.

Both strategies below are textbook baselines. They exist so you have something
measurable to beat -- not because they are expected to be profitable. Published
simple technical rules are the most heavily arbitraged signals in existence.
Treat a positive backtest here as a bug until you have proven otherwise.
"""

from abc import ABC, abstractmethod
from typing import List, Optional

from bot.models import Bar, Signal, SignalType


def sma(values: List[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def stdev(values: List[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    window = values[-n:]
    mean = sum(window) / n
    var = sum((v - mean) ** 2 for v in window) / n
    return var ** 0.5


class Strategy(ABC):
    name = "base"

    @property
    @abstractmethod
    def lookback(self) -> int:
        """How many bars this strategy needs before it can say anything."""

    @abstractmethod
    def evaluate(self, bars: List[Bar], holding: bool) -> Signal:
        """Given bar history (oldest first), decide. `holding` = we own it."""


class SmaCrossover(Strategy):
    """Enter when fast SMA crosses above slow SMA, exit on the reverse cross."""

    name = "sma_crossover"

    def __init__(self, fast: int = 10, slow: int = 30):
        if fast >= slow:
            raise ValueError("fast period must be shorter than slow period")
        self.fast, self.slow = fast, slow

    @property
    def lookback(self) -> int:
        return self.slow + 2

    def evaluate(self, bars: List[Bar], holding: bool) -> Signal:
        sym = bars[-1].symbol if bars else "?"
        closes = [b.close for b in bars]
        if len(closes) < self.slow + 1:
            return Signal(sym, SignalType.HOLD, "warming up")

        fast_now, slow_now = sma(closes, self.fast), sma(closes, self.slow)
        fast_prev = sma(closes[:-1], self.fast)
        slow_prev = sma(closes[:-1], self.slow)
        if None in (fast_now, slow_now, fast_prev, slow_prev):
            return Signal(sym, SignalType.HOLD, "warming up")

        crossed_up = fast_prev <= slow_prev and fast_now > slow_now
        crossed_down = fast_prev >= slow_prev and fast_now < slow_now

        if crossed_up and not holding:
            return Signal(sym, SignalType.ENTER_LONG, f"SMA{self.fast} crossed above SMA{self.slow}")
        if crossed_down and holding:
            return Signal(sym, SignalType.EXIT_LONG, f"SMA{self.fast} crossed below SMA{self.slow}")
        return Signal(sym, SignalType.HOLD, "no cross")


class MeanReversion(Strategy):
    """Buy when price is stretched below its mean, sell when it snaps back."""

    name = "mean_reversion"

    def __init__(self, window: int = 20, entry_z: float = -2.0, exit_z: float = -0.2):
        self.window, self.entry_z, self.exit_z = window, entry_z, exit_z

    @property
    def lookback(self) -> int:
        return self.window + 2

    def _zscore(self, closes: List[float]) -> Optional[float]:
        mean, sd = sma(closes, self.window), stdev(closes, self.window)
        if mean is None or not sd:
            return None
        return (closes[-1] - mean) / sd

    def evaluate(self, bars: List[Bar], holding: bool) -> Signal:
        sym = bars[-1].symbol if bars else "?"
        closes = [b.close for b in bars]
        z = self._zscore(closes)
        if z is None:
            return Signal(sym, SignalType.HOLD, "warming up")

        if not holding and z <= self.entry_z:
            conf = min(1.0, abs(z) / abs(self.entry_z * 1.5))
            return Signal(sym, SignalType.ENTER_LONG, f"z={z:.2f} below entry", conf)
        if holding and z >= self.exit_z:
            return Signal(sym, SignalType.EXIT_LONG, f"z={z:.2f} reverted")
        return Signal(sym, SignalType.HOLD, f"z={z:.2f}")


REGISTRY = {
    SmaCrossover.name: SmaCrossover,
    MeanReversion.name: MeanReversion,
}
