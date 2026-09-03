"""Every decision gets written down. Without this you cannot tell whether a
losing week was bad luck or a bug -- and those need opposite responses.
"""

import csv
import os
from datetime import datetime, timezone
from typing import Optional

from bot.models import Fill, Signal


class Journal:
    FILL_COLS = ["timestamp", "symbol", "side", "qty", "price", "commission", "order_id", "reason"]
    EQUITY_COLS = ["timestamp", "equity", "cash", "open_positions", "day_trades_used"]

    def __init__(self, directory: str = "logs"):
        os.makedirs(directory, exist_ok=True)
        self.fills_path = os.path.join(directory, "fills.csv")
        self.equity_path = os.path.join(directory, "equity.csv")
        self._ensure(self.fills_path, self.FILL_COLS)
        self._ensure(self.equity_path, self.EQUITY_COLS)

    @staticmethod
    def _ensure(path: str, cols) -> None:
        if not os.path.exists(path):
            with open(path, "w", newline="") as f:
                csv.writer(f).writerow(cols)

    def record_fill(self, fill: Fill, reason: str = "") -> None:
        with open(self.fills_path, "a", newline="") as f:
            csv.writer(f).writerow([
                fill.timestamp.isoformat(), fill.symbol, fill.side.value,
                f"{fill.qty:.6f}", f"{fill.price:.4f}",
                f"{fill.commission:.4f}", fill.order_id, reason,
            ])

    def record_equity(self, equity: float, cash: float, open_positions: int,
                      day_trades_used: int) -> None:
        with open(self.equity_path, "a", newline="") as f:
            csv.writer(f).writerow([
                datetime.now(timezone.utc).isoformat(),
                f"{equity:.4f}", f"{cash:.4f}", open_positions, day_trades_used,
            ])
