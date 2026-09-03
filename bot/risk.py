"""The layer that stands between a strategy's enthusiasm and your account.

Every order passes through here. A strategy can never place an order directly,
and there is deliberately no way to configure these checks off.
"""

import logging
import os
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Deque, Dict, List, Optional, Tuple

from bot.models import AccountState, Order, Position, Side, Signal, SignalType

log = logging.getLogger(__name__)


@dataclass
class RiskLimits:
    max_position_pct: float = 0.25      # max fraction of equity in one name
    max_open_positions: int = 3
    max_daily_loss_pct: float = 0.05    # halt the bot for the day at -5%
    stop_loss_pct: float = 0.03         # per-position hard stop
    take_profit_pct: float = 0.06       # per-position target
    min_order_value: float = 1.00       # skip dust orders
    max_day_trades_per_5d: int = 3      # PDT rule for accounts under $25k
    pdt_enforcement: bool = True
    kill_switch_file: str = "HALT"


@dataclass
class RiskState:
    day: date = field(default_factory=lambda: datetime.now(timezone.utc).date())
    day_start_equity: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    # (date_opened, symbol) pairs for round trips closed same day
    day_trades: Deque[Tuple[date, str]] = field(default_factory=deque)
    opened_today: Dict[str, date] = field(default_factory=dict)


class RiskManager:
    def __init__(self, limits: RiskLimits):
        self.limits = limits
        self.state = RiskState()

    # -- lifecycle ---------------------------------------------------------

    def start_session(self, equity: float) -> None:
        today = datetime.now(timezone.utc).date()
        if self.state.day != today or self.state.day_start_equity == 0.0:
            self.state.day = today
            self.state.day_start_equity = equity
            self.state.halted = False
            self.state.halt_reason = ""
            self.state.opened_today.clear()
            log.info("New session. Starting equity: %.2f", equity)

    def kill_switch_tripped(self) -> bool:
        """Create a file named HALT in the working dir to stop the bot dead."""
        return os.path.exists(self.limits.kill_switch_file)

    def check_halt(self, account: AccountState) -> bool:
        if self.kill_switch_tripped():
            self._halt("kill switch file present")
            return True
        if self.state.halted:
            return True
        if self.state.day_start_equity > 0:
            dd = (account.equity - self.state.day_start_equity) / self.state.day_start_equity
            if dd <= -abs(self.limits.max_daily_loss_pct):
                self._halt(f"daily loss limit hit ({dd:.2%})")
                return True
        return False

    def _halt(self, reason: str) -> None:
        if not self.state.halted:
            log.error("TRADING HALTED: %s", reason)
        self.state.halted = True
        self.state.halt_reason = reason

    # -- day trade accounting ---------------------------------------------

    def _prune_day_trades(self) -> None:
        today = datetime.now(timezone.utc).date()
        while self.state.day_trades and (today - self.state.day_trades[0][0]).days >= 5:
            self.state.day_trades.popleft()

    def day_trades_used(self) -> int:
        self._prune_day_trades()
        return len(self.state.day_trades)

    def record_entry(self, symbol: str) -> None:
        self.state.opened_today[symbol] = datetime.now(timezone.utc).date()

    def record_exit(self, symbol: str) -> None:
        today = datetime.now(timezone.utc).date()
        if self.state.opened_today.get(symbol) == today:
            self.state.day_trades.append((today, symbol))
            log.warning(
                "Day trade recorded on %s (%d/%d used in rolling 5 days)",
                symbol, self.day_trades_used(), self.limits.max_day_trades_per_5d,
            )
        self.state.opened_today.pop(symbol, None)

    def _would_be_day_trade(self, symbol: str) -> bool:
        return self.state.opened_today.get(symbol) == datetime.now(timezone.utc).date()

    # -- protective exits --------------------------------------------------

    def protective_exit(self, pos: Position, price: float) -> Optional[str]:
        """Stop loss / take profit. Checked before the strategy gets a vote."""
        if not pos.is_open or pos.avg_cost <= 0:
            return None
        change = (price - pos.avg_cost) / pos.avg_cost
        if change <= -abs(self.limits.stop_loss_pct):
            return f"stop loss ({change:.2%})"
        if change >= abs(self.limits.take_profit_pct):
            return f"take profit ({change:.2%})"
        return None

    # -- sizing ------------------------------------------------------------

    def size_order(
        self, signal: Signal, account: AccountState, price: float,
        allow_fractional: bool = True, protective: bool = False,
    ) -> Optional[Order]:
        """Turn a signal into a concrete order, or return None if it's blocked.

        `protective` marks stop-loss / take-profit exits. Those bypass the PDT
        counter: letting a stop fail because of a day-trade budget is a far
        worse outcome than getting flagged. Strategy exits do get blocked.
        """
        sym = signal.symbol
        pos = account.positions.get(sym, Position(symbol=sym))

        if signal.type == SignalType.EXIT_LONG:
            if not pos.is_open:
                return None
            over_pdt = (
                self.limits.pdt_enforcement
                and self._would_be_day_trade(sym)
                and self.day_trades_used() >= self.limits.max_day_trades_per_5d
            )
            if over_pdt and not protective:
                log.warning("Blocking discretionary exit on %s: day trade limit reached", sym)
                return None
            if over_pdt and protective:
                log.error(
                    "Protective exit on %s overrides day trade limit -- "
                    "a real account would be flagged as a Pattern Day Trader here",
                    sym,
                )
            return Order(symbol=sym, side=Side.SELL, qty=pos.qty)

        if signal.type != SignalType.ENTER_LONG:
            return None

        if pos.is_open:
            return None  # no pyramiding
        open_count = sum(1 for p in account.positions.values() if p.is_open)
        if open_count >= self.limits.max_open_positions:
            log.info("Skipping %s: at max open positions (%d)", sym, open_count)
            return None

        budget = min(
            account.equity * self.limits.max_position_pct * max(0.0, min(1.0, signal.confidence)),
            account.cash,
        )
        if budget < self.limits.min_order_value or price <= 0:
            log.info("Skipping %s: budget %.2f below minimum", sym, budget)
            return None

        qty = budget / price
        if not allow_fractional:
            qty = float(int(qty))
            if qty < 1:
                log.info("Skipping %s: cannot afford 1 whole share at %.2f", sym, price)
                return None
        return Order(symbol=sym, side=Side.BUY, qty=round(qty, 5))
