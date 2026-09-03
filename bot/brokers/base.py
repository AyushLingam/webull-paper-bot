"""Broker abstraction.

Every broker -- simulated or real -- implements this interface, so the engine
never knows or cares which one it is talking to. Swapping paper for live is a
one-line config change, which is exactly why the risk layer sits above this.
"""

from abc import ABC, abstractmethod
from typing import Dict, Optional

from bot.models import AccountState, Fill, Order, Position


class Broker(ABC):
    @abstractmethod
    def get_account(self) -> AccountState:
        """Cash, total equity, and open positions."""

    @abstractmethod
    def get_position(self, symbol: str) -> Position:
        """Current position in one symbol (qty 0 if flat)."""

    @abstractmethod
    def submit(self, order: Order, ref_price: float) -> Optional[Fill]:
        """Send an order. Returns a Fill if it filled immediately, else None.

        ref_price is the last known price, used by simulated brokers to model
        the fill and by live brokers only for logging.
        """

    @abstractmethod
    def mark_to_market(self, prices: Dict[str, float]) -> None:
        """Update equity given current prices. No-op for live brokers."""

    @property
    @abstractmethod
    def name(self) -> str:
        ...
