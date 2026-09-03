"""Core data types passed between the feed, strategy, risk, and broker layers."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class SignalType(str, Enum):
    ENTER_LONG = "ENTER_LONG"
    EXIT_LONG = "EXIT_LONG"
    HOLD = "HOLD"


@dataclass(frozen=True)
class Bar:
    """One OHLCV candle for one symbol."""
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Signal:
    """A strategy's opinion. It is NOT an order -- risk.py decides sizing."""
    symbol: str
    type: SignalType
    reason: str = ""
    confidence: float = 1.0  # 0..1, used only for scaling position size


@dataclass
class Order:
    symbol: str
    side: Side
    qty: float
    order_type: str = "MARKET"
    limit_price: Optional[float] = None
    client_order_id: str = ""


@dataclass
class Fill:
    symbol: str
    side: Side
    qty: float
    price: float
    timestamp: datetime
    commission: float = 0.0
    order_id: str = ""


@dataclass
class Position:
    symbol: str
    qty: float = 0.0
    avg_cost: float = 0.0

    @property
    def is_open(self) -> bool:
        return abs(self.qty) > 1e-9

    def market_value(self, price: float) -> float:
        return self.qty * price

    def unrealized_pnl(self, price: float) -> float:
        return (price - self.avg_cost) * self.qty


@dataclass
class AccountState:
    cash: float
    equity: float
    positions: dict = field(default_factory=dict)  # symbol -> Position
