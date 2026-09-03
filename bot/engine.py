"""The loop. Deliberately boring: fetch -> evaluate -> risk-check -> execute.

Order of operations matters. Protective exits are evaluated before strategy
signals, so a stop loss always beats a strategy saying "hold".
"""

import logging
import time
from typing import Dict, List

from bot.brokers.base import Broker
from bot.data.feeds import DataFeed
from bot.journal import Journal
from bot.models import Signal, SignalType
from bot.risk import RiskManager
from bot.strategies.starters import Strategy

log = logging.getLogger(__name__)


class Engine:
    def __init__(
        self,
        broker: Broker,
        feed: DataFeed,
        strategy: Strategy,
        risk: RiskManager,
        symbols: List[str],
        journal: Journal,
        poll_seconds: int = 60,
        allow_fractional: bool = True,
    ):
        self.broker = broker
        self.feed = feed
        self.strategy = strategy
        self.risk = risk
        self.symbols = symbols
        self.journal = journal
        self.poll_seconds = poll_seconds
        self.allow_fractional = allow_fractional

    def tick(self) -> None:
        bars_by_symbol = self.feed.latest_bars(self.symbols, self.strategy.lookback)
        prices: Dict[str, float] = {
            s: bars[-1].close for s, bars in bars_by_symbol.items() if bars
        }
        if not prices:
            log.warning("No price data this tick; skipping")
            return

        self.broker.mark_to_market(prices)
        account = self.broker.get_account()
        self.risk.start_session(account.equity)

        if self.risk.check_halt(account):
            log.info("Halted (%s) -- no new orders", self.risk.state.halt_reason)
            return

        for symbol in self.symbols:
            bars = bars_by_symbol.get(symbol) or []
            if not bars:
                continue
            price = prices[symbol]
            position = account.positions.get(symbol)
            holding = bool(position and position.is_open)

            # 1. Protective exits win over everything.
            reason = None
            if holding:
                reason = self.risk.protective_exit(position, price)
            protective = reason is not None

            if protective:
                signal = Signal(symbol, SignalType.EXIT_LONG, reason)
            else:
                signal = self.strategy.evaluate(bars, holding)
                reason = signal.reason

            if signal.type == SignalType.HOLD:
                continue

            order = self.risk.size_order(
                signal, account, price, self.allow_fractional, protective=protective
            )
            if not order:
                continue

            fill = self.broker.submit(order, price)
            if signal.type == SignalType.ENTER_LONG:
                self.risk.record_entry(symbol)
            else:
                self.risk.record_exit(symbol)

            if fill:
                self.journal.record_fill(fill, reason)
            account = self.broker.get_account()  # refresh after each action

        self.broker.mark_to_market(prices)
        final = self.broker.get_account()
        self.journal.record_equity(
            final.equity, final.cash,
            sum(1 for p in final.positions.values() if p.is_open),
            self.risk.day_trades_used(),
        )

    def run(self, max_ticks: int = 0) -> None:
        log.info(
            "Engine starting | broker=%s strategy=%s symbols=%s",
            self.broker.name, self.strategy.name, ",".join(self.symbols),
        )
        ticks = 0
        try:
            while True:
                self.tick()
                ticks += 1
                if max_ticks and ticks >= max_ticks:
                    log.info("Reached max_ticks=%d, stopping", max_ticks)
                    break
                if self.risk.state.halted and self.risk.kill_switch_tripped():
                    log.info("Kill switch active, exiting")
                    break
                time.sleep(self.poll_seconds)
        except KeyboardInterrupt:
            log.info("Interrupted by user")
        finally:
            acct = self.broker.get_account()
            log.info(
                "Final: equity=%.2f cash=%.2f open=%d",
                acct.equity, acct.cash,
                sum(1 for p in acct.positions.values() if p.is_open),
            )
