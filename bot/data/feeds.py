"""Market data feeds.

SyntheticFeed lets you run the entire bot end-to-end with zero credentials and
zero network. Use it to prove the plumbing works before you plug in real data.
"""

import logging
import math
import random
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from bot.models import Bar

log = logging.getLogger(__name__)


class DataFeed(ABC):
    @abstractmethod
    def latest_bars(self, symbols: List[str], lookback: int) -> Dict[str, List[Bar]]:
        """Most recent `lookback` bars per symbol, oldest first."""


class SyntheticFeed(DataFeed):
    """Geometric random walk with a configurable mean-reverting component.

    Every price series here is fake. If a strategy makes money on this, that
    proves the code runs -- it proves nothing at all about the strategy.
    """

    def __init__(self, symbols: List[str], seed: int = 42, start_price: float = 50.0,
                 vol: float = 0.012, mean_reversion: float = 0.05):
        self.rng = random.Random(seed)
        self.vol = vol
        self.mean_reversion = mean_reversion
        self.anchor = {s: start_price for s in symbols}
        self.price = {s: start_price for s in symbols}
        self.history: Dict[str, List[Bar]] = {s: [] for s in symbols}
        self._t = datetime.now(timezone.utc) - timedelta(minutes=500)
        for _ in range(300):
            self._step(symbols)

    def _step(self, symbols: List[str]) -> None:
        self._t += timedelta(minutes=1)
        for s in symbols:
            p = self.price[s]
            pull = self.mean_reversion * (self.anchor[s] - p) / max(self.anchor[s], 1e-9)
            shock = self.rng.gauss(0, self.vol)
            new_p = max(0.5, p * math.exp(pull + shock))
            high = max(p, new_p) * (1 + abs(self.rng.gauss(0, 0.001)))
            low = min(p, new_p) * (1 - abs(self.rng.gauss(0, 0.001)))
            self.history[s].append(Bar(
                symbol=s, timestamp=self._t, open=p, high=high,
                low=low, close=new_p, volume=self.rng.randint(1000, 50000),
            ))
            self.price[s] = new_p

    def latest_bars(self, symbols: List[str], lookback: int) -> Dict[str, List[Bar]]:
        self._step(symbols)
        return {s: self.history[s][-lookback:] for s in symbols}


class WebullFeed(DataFeed):
    """Real OHLCV bars from Webull's market data API (official SDK v2)."""

    def __init__(self, app_key: str, app_secret: str, region: str = "us",
                 timespan: str = "M1", endpoint: str = "api.sandbox.webull.com"):
        try:
            from webull.core.client import ApiClient
            from webull.data.data_client import DataClient
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "Run: pip install --upgrade webull-openapi-python-sdk"
            ) from exc
        self._client = ApiClient(app_key, app_secret, region)
        if endpoint:
            self._client.add_endpoint(region, endpoint)
        log.info("Webull feed -> %s", endpoint or "default")
        try:
            self._data = DataClient(self._client)
        except Exception as exc:
            raise ConnectionError(
                f"Could not initialise Webull data client against '{endpoint}'.\n"
                f"  {type(exc).__name__}: {str(exc)[:200]}"
            ) from exc
        self.timespan = timespan
        self.category = "US_STOCK"

    def latest_bars(self, symbols: List[str], lookback: int) -> Dict[str, List[Bar]]:
        out: Dict[str, List[Bar]] = {}
        for sym in symbols:
            try:
                res = self._data.market_data.get_history_bar(
                    sym, self.category, self.timespan,
                    count=str(max(lookback, 10)),
                )
                if res.status_code != 200:
                    log.error("Bars for %s failed (%s): %s",
                              sym, res.status_code, res.text[:300])
                    out[sym] = []
                    continue
                out[sym] = self._parse(sym, res.json())
            except Exception:
                log.exception("Bar fetch failed for %s", sym)
                out[sym] = []
        return out

    @staticmethod
    def _parse(sym: str, payload) -> List[Bar]:
        """Response shape varies, so probe rather than assume."""
        rows = []
        if isinstance(payload, dict):
            rows = payload.get("bars") or payload.get("data") or []
            if rows and isinstance(rows[0], dict) and "bars" in rows[0]:
                rows = rows[0]["bars"]
        elif isinstance(payload, list):
            rows = payload[0].get("bars", payload) if (
                payload and isinstance(payload[0], dict) and "bars" in payload[0]
            ) else payload

        bars: List[Bar] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            ts = r.get("tradeTime") or r.get("timeStamp") or r.get("time")
            stamp = None
            try:
                if ts is None:
                    continue
                s = str(ts)
                if s.isdigit():
                    v = int(s)
                    if v > 1e11:      # milliseconds
                        v //= 1000
                    stamp = datetime.fromtimestamp(v, tz=timezone.utc)
                else:
                    stamp = datetime.fromisoformat(s.replace("Z", "+00:00"))
                bars.append(Bar(
                    symbol=sym, timestamp=stamp,
                    open=float(r["open"]), high=float(r["high"]),
                    low=float(r["low"]), close=float(r["close"]),
                    volume=float(r.get("volume") or 0),
                ))
            except (ValueError, TypeError, KeyError):
                continue

        bars.sort(key=lambda b: b.timestamp)  # API returns newest-first
        if not bars:
            log.warning("No usable bars for %s. Raw: %s", sym, str(payload)[:400])
        return bars


class ReplayFeed(DataFeed):
    """Replays a fixed set of historical bars one step at a time.

    This is what makes a backtest honest: the strategy sees exactly the same
    interface it sees live, and it can only ever see bars up to the current
    cursor. It is structurally impossible for it to peek at future prices,
    which is the single most common way backtests lie.
    """

    def __init__(self, bars_by_symbol: Dict[str, List[Bar]], warmup: int = 0):
        self.bars = {s: sorted(b, key=lambda x: x.timestamp)
                     for s, b in bars_by_symbol.items() if b}
        self.cursor = max(1, warmup)
        self.length = max((len(b) for b in self.bars.values()), default=0)

    @property
    def exhausted(self) -> bool:
        return self.cursor >= self.length

    @property
    def steps_remaining(self) -> int:
        return max(0, self.length - self.cursor)

    def current_time(self):
        for bars in self.bars.values():
            if self.cursor <= len(bars):
                return bars[self.cursor - 1].timestamp
        return None

    def latest_bars(self, symbols: List[str], lookback: int) -> Dict[str, List[Bar]]:
        out: Dict[str, List[Bar]] = {}
        for s in symbols:
            series = self.bars.get(s) or []
            window = series[:self.cursor]
            out[s] = window[-lookback:] if lookback else window
        self.cursor += 1
        return out


def bars_from_csv(path: str) -> Dict[str, List[Bar]]:
    """Load bars from a CSV with columns: symbol,timestamp,open,high,low,close,volume"""
    import csv as _csv
    out: Dict[str, List[Bar]] = {}
    with open(path, newline="") as f:
        for row in _csv.DictReader(f):
            try:
                out.setdefault(row["symbol"], []).append(Bar(
                    symbol=row["symbol"],
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                    open=float(row["open"]), high=float(row["high"]),
                    low=float(row["low"]), close=float(row["close"]),
                    volume=float(row.get("volume") or 0),
                ))
            except (ValueError, KeyError):
                continue
    for s in out:
        out[s].sort(key=lambda b: b.timestamp)
    return out


def bars_to_csv(path: str, bars_by_symbol: Dict[str, List[Bar]]) -> int:
    import csv as _csv
    n = 0
    with open(path, "w", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["symbol", "timestamp", "open", "high", "low", "close", "volume"])
        for sym, bars in sorted(bars_by_symbol.items()):
            for b in bars:
                w.writerow([b.symbol, b.timestamp.isoformat(), b.open,
                            b.high, b.low, b.close, b.volume])
                n += 1
    return n
