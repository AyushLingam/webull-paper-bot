"""A local paper broker. No network, no credentials, no account required.

This is deliberately pessimistic: it charges you slippage on every fill and
refuses orders you cannot afford. A strategy that only looks good with zero
slippage is a strategy that loses money in reality.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, Optional

from bot.brokers.base import Broker
from bot.models import AccountState, Fill, Order, Position, Side

log = logging.getLogger(__name__)


class SimulatedBroker(Broker):
    def __init__(
        self,
        starting_cash: float = 100.0,
        slippage_bps: float = 5.0,
        commission_per_trade: float = 0.0,
        allow_fractional: bool = True,
    ):
        self.cash = starting_cash
        self.starting_cash = starting_cash
        self.slippage_bps = slippage_bps
        self.commission_per_trade = commission_per_trade
        self.allow_fractional = allow_fractional
        self._positions: Dict[str, Position] = {}
        self._equity = starting_cash
        self.realized_pnl = 0.0
        self.fills = []

    @property
    def name(self) -> str:
        return "simulated"

    def get_account(self) -> AccountState:
        return AccountState(
            cash=self.cash,
            equity=self._equity,
            positions={s: p for s, p in self._positions.items() if p.is_open},
        )

    def get_position(self, symbol: str) -> Position:
        return self._positions.setdefault(symbol, Position(symbol=symbol))

    def _fill_price(self, side: Side, ref_price: float) -> float:
        """Slippage always works against you."""
        drift = ref_price * (self.slippage_bps / 10_000.0)
        return ref_price + drift if side == Side.BUY else ref_price - drift

    def submit(self, order: Order, ref_price: float) -> Optional[Fill]:
        if ref_price <= 0:
            log.warning("Rejecting %s: bad reference price %s", order.symbol, ref_price)
            return None

        qty = order.qty
        if not self.allow_fractional:
            qty = float(int(qty))
        if qty <= 0:
            return None

        price = self._fill_price(order.side, ref_price)
        pos = self.get_position(order.symbol)

        if order.side == Side.BUY:
            cost = qty * price + self.commission_per_trade
            if cost > self.cash:
                # Shrink to what we can actually afford rather than silently failing.
                affordable = (self.cash - self.commission_per_trade) / price
                if affordable <= 0:
                    log.warning("Insufficient cash for %s (have %.2f)", order.symbol, self.cash)
                    return None
                qty = affordable
                cost = qty * price + self.commission_per_trade
            new_qty = pos.qty + qty
            pos.avg_cost = ((pos.avg_cost * pos.qty) + (price * qty)) / new_qty
            pos.qty = new_qty
            self.cash -= cost
        else:
            qty = min(qty, pos.qty)  # never short in this bot
            if qty <= 1e-9:
                return None
            proceeds = qty * price - self.commission_per_trade
            self.realized_pnl += (price - pos.avg_cost) * qty - self.commission_per_trade
            pos.qty -= qty
            if not pos.is_open:
                pos.qty = 0.0
                pos.avg_cost = 0.0
            self.cash += proceeds

        fill = Fill(
            symbol=order.symbol,
            side=order.side,
            qty=qty,
            price=price,
            timestamp=datetime.now(timezone.utc),
            commission=self.commission_per_trade,
            order_id=order.client_order_id,
        )
        self.fills.append(fill)
        log.info("FILL %s %s %.4f @ %.4f", order.side.value, order.symbol, qty, price)
        return fill

    def mark_to_market(self, prices: Dict[str, float]) -> None:
        holdings = sum(
            p.market_value(prices.get(s, p.avg_cost))
            for s, p in self._positions.items()
            if p.is_open
        )
        self._equity = self.cash + holdings
