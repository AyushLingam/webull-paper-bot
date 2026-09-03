"""Webull OpenAPI adapter.

Built against the CURRENT official SDK: `webull-openapi-python-sdk` (module
`webull.*`), which supports Python 3.8-3.14.

Do NOT use the older `webull-python-sdk-trade` / `webullsdkcore` packages. They
are deprecated, pin a 2022 grpcio that has no wheels past Python 3.11, and
their endpoint paths return 404 against the current sandbox host.

Endpoints:
    sandbox / paper -> api.sandbox.webull.com
    production      -> api.webull.com
"""

import logging
import uuid
from typing import Dict, Optional

from bot.brokers.base import Broker
from bot.models import AccountState, Fill, Order, Position, Side

log = logging.getLogger(__name__)


class WebullBroker(Broker):
    def __init__(self, app_key: str, app_secret: str, account_id: str,
                 region: str = "us", endpoint: str = "api.sandbox.webull.com"):
        try:
            from webull.core.client import ApiClient
            from webull.trade.trade_client import TradeClient
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "Webull SDK missing or wrong version. Run:\n"
                "  pip install --upgrade webull-openapi-python-sdk\n"
                "If you previously installed webull-python-sdk-trade, uninstall it."
            ) from exc

        self.account_id = account_id
        self.market = "US" if region.lower() == "us" else region.upper()
        self._client = ApiClient(app_key, app_secret, region)
        if endpoint:
            self._client.add_endpoint(region, endpoint)
        log.info("Webull broker -> %s (region=%s)", endpoint or "default", region)
        try:
            # NOTE: TradeClient contacts /openapi/config during construction,
            # so a bad endpoint or no network fails here, not on first use.
            self._trade = TradeClient(self._client)
        except Exception as exc:
            raise ConnectionError(
                f"Could not initialise Webull client against '{endpoint}'.\n"
                f"  {type(exc).__name__}: {str(exc)[:200]}\n"
                "Check the endpoint is reachable and your key/secret are valid."
            ) from exc

    @property
    def name(self) -> str:
        return "webull"

    # -- diagnostics -------------------------------------------------------

    def check_connection(self) -> bool:
        """Verify credentials and list accounts. Run this before trading."""
        try:
            res = self._trade.account_v2.get_account_list()
        except Exception:
            log.exception("Could not reach Webull")
            return False
        if res.status_code != 200:
            log.error("Account list failed (%s): %s", res.status_code, res.text[:400])
            return False

        payload = res.json()
        rows = payload if isinstance(payload, list) else (
            payload.get("data") or payload.get("accounts") or []
        )
        ids = []
        for r in rows if isinstance(rows, list) else []:
            if isinstance(r, dict):
                aid = r.get("account_id") or r.get("accountId") or r.get("id")
                if aid:
                    ids.append((
                        str(aid),
                        r.get("account_type") or r.get("type") or "?",
                        r.get("account_number") or "?",
                        r.get("account_label") or "",
                    ))

        log.info("Connected to Webull.")
        if ids:
            log.info("Accounts available to this app key:")
            log.info("  NOTE: use account_id (long), not the account_number")
            log.info("        shown in the Webull UI.")
            for aid, atype, num, label in ids:
                marker = "  <-- WEBULL_ACCOUNT_ID" if aid == self.account_id else ""
                log.info("    %s  %-18s (no. %s)%s", aid, label or atype, num, marker)
            known = [a[0] for a in ids]
            if self.account_id not in known:
                by_number = [a for a in ids if a[2] == self.account_id]
                if by_number:
                    log.error(
                        "WEBULL_ACCOUNT_ID='%s' is an account NUMBER, not an ID. "
                        "Use '%s' instead (that's the %s account).",
                        self.account_id, by_number[0][0],
                        by_number[0][3] or by_number[0][1],
                    )
                else:
                    log.error(
                        "WEBULL_ACCOUNT_ID='%s' is NOT in that list. "
                        "Copy one of the long IDs above into your .env file.",
                        self.account_id,
                    )
                return False
        else:
            log.warning("Could not parse account list. Raw: %s", str(payload)[:400])
        return True

    # -- Broker interface --------------------------------------------------

    def get_account(self) -> AccountState:
        cash = equity = 0.0
        positions: Dict[str, Position] = {}

        try:
            res = self._trade.account_v2.get_account_balance(self.account_id)
            if res.status_code == 200:
                d = res.json() or {}
                # Verified response shape (US sandbox, 2026-08):
                #   {"total_market_value": "...",
                #    "account_currency_assets": [
                #       {"currency": "USD", "market_value": "...",
                #        "buying_power": "...", ...}]}
                assets = d.get("account_currency_assets") or []
                usd = {}
                for a in assets:
                    if a.get("currency", "USD") == "USD":
                        usd = a
                        break
                if not usd and assets:
                    usd = assets[0]

                cash = _first_float(usd, [
                    "buying_power", "cash_balance", "settled_funds",
                    "available_buying_power",
                ])
                market_value = _first_float(usd, ["market_value"]) or \
                    _first_float(d, ["total_market_value"])
                equity = cash + market_value
                if not assets:
                    log.warning("Unrecognised balance shape: %s", str(d)[:400])
            else:
                log.error("Balance failed (%s): %s", res.status_code, res.text[:400])
        except Exception:
            log.exception("Balance fetch failed")

        try:
            res = self._trade.account_v2.get_account_position(self.account_id)
            if res.status_code == 200:
                payload = res.json()
                # Verified: this endpoint returns a bare JSON list.
                if isinstance(payload, list):
                    rows = payload
                elif isinstance(payload, dict):
                    rows = (payload.get("holdings") or payload.get("positions")
                            or payload.get("items") or [])
                else:
                    rows = []
                for item in rows:
                    sym = item.get("symbol")
                    if not sym:
                        continue
                    positions[sym] = Position(
                        symbol=sym,
                        qty=float(item.get("quantity") or item.get("qty") or 0),
                        avg_cost=float(
                            item.get("cost_price") or item.get("avg_price")
                            or item.get("average_cost") or item.get("cost") or 0
                        ),
                    )
            else:
                log.error("Positions failed (%s): %s", res.status_code, res.text[:400])
        except Exception:
            log.exception("Position fetch failed")

        return AccountState(cash=cash, equity=equity, positions=positions)

    def get_position(self, symbol: str) -> Position:
        return self.get_account().positions.get(symbol, Position(symbol=symbol))

    def submit(self, order: Order, ref_price: float) -> Optional[Fill]:
        client_order_id = order.client_order_id or uuid.uuid4().hex
        payload = {
            "client_order_id": client_order_id,
            "symbol": order.symbol,
            "instrument_type": "EQUITY",
            "market": self.market,
            "order_type": order.order_type,
            "quantity": str(order.qty),
            "support_trading_session": "CORE",
            "side": order.side.value,
            "time_in_force": "DAY",
            "entrust_type": "QTY",
        }
        if order.order_type == "LIMIT":
            price = order.limit_price or ref_price
            payload["limit_price"] = str(round(price, 2))

        try:
            res = self._trade.order_v2.place_order(self.account_id, [payload])
        except Exception:
            log.exception("Order submission raised for %s", order.symbol)
            return None

        if res.status_code != 200:
            log.error("Order rejected (%s): %s", res.status_code, res.text[:400])
            return None

        log.info("SUBMITTED %s %s %.4f (id %s)",
                 order.side.value, order.symbol, order.qty, client_order_id)
        # Orders are asynchronous. The engine reconciles real fills from
        # get_account() on the next tick rather than assuming this filled.
        return None

    def mark_to_market(self, prices: Dict[str, float]) -> None:
        return  # Webull tracks this server-side


def _first_float(d: dict, keys) -> float:
    """Return the first key present and numeric, else 0.0."""
    for k in keys:
        if k in d and d[k] not in (None, ""):
            try:
                return float(d[k])
            except (TypeError, ValueError):
                continue
    return 0.0
