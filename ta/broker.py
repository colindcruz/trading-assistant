"""
The broker seam. Everything that talks to Kite goes through a Broker so the exit
engine and the service can run against a FakeBroker in tests and a DryRunBroker in
the market.

Order placement lessons carried over from the old code:
- Never a raw MARKET order on options: Kite rejects it ("Market orders without market
  protection are not allowed via API", hit for real 2026-08-24; mandatory since 2026-04-01).
- Prices must be tick multiples (see exit_logic.round_to_tick).
- validity DAY, variety regular, product = the position's own product.
- Tags are alphanumeric, max 20 chars, and come back on order rows; they are how the
  TA tells its own orders apart from anything else resting in the book.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Protocol

log = logging.getLogger("ta.broker")


class Broker(Protocol):
    def positions_net(self) -> list[dict]: ...
    def orders(self) -> list[dict]: ...
    def trades(self) -> list[dict]: ...
    def ltp(self, keys: list[str]) -> dict[str, float]: ...
    def best_bid(self, keys: list[str]) -> dict[str, float]: ...
    def margins_equity(self) -> dict: ...
    def instruments(self, exchange: str) -> list[dict]: ...
    def place_limit_sell(self, exchange: str, tradingsymbol: str, product: str, qty: int, price: float, tag: str) -> str: ...
    def place_market_sell(self, exchange: str, tradingsymbol: str, product: str, qty: int, tag: str, market_protection: int) -> str: ...
    def modify_price(self, order_id: str, price: float, variety: str = "regular") -> None: ...
    def cancel(self, order_id: str, variety: str = "regular") -> None: ...


class KiteBroker:
    """Thin adapter over kiteconnect.KiteConnect."""

    def __init__(self, kite):
        self.kite = kite

    def positions_net(self) -> list[dict]:
        return self.kite.positions()["net"]

    def orders(self) -> list[dict]:
        return self.kite.orders()

    def trades(self) -> list[dict]:
        return self.kite.trades()

    def ltp(self, keys: list[str]) -> dict[str, float]:
        if not keys:
            return {}
        data = self.kite.ltp(keys)
        return {k: float(v["last_price"]) for k, v in data.items()}

    def best_bid(self, keys: list[str]) -> dict[str, float]:
        """Top of the buy side of the book, from kite.quote (full depth). Falls back
        to LTP for any key whose depth is empty."""
        if not keys:
            return {}
        out = {}
        data = self.kite.quote(keys)
        for k, v in data.items():
            bids = ((v.get("depth") or {}).get("buy") or [])
            price = next((float(b["price"]) for b in bids if float(b.get("price") or 0) > 0), 0.0)
            if price <= 0:
                price = float(v.get("last_price") or 0)
            out[k] = price
        return out

    def margins_equity(self) -> dict:
        return self.kite.margins("equity")

    def instruments(self, exchange: str) -> list[dict]:
        return self.kite.instruments(exchange)

    def place_limit_sell(self, exchange, tradingsymbol, product, qty, price, tag) -> str:
        return str(self.kite.place_order(
            variety="regular", exchange=exchange, tradingsymbol=tradingsymbol,
            transaction_type="SELL", quantity=int(qty), product=product,
            order_type="LIMIT", price=price, validity="DAY", tag=tag))

    def place_market_sell(self, exchange, tradingsymbol, product, qty, tag, market_protection) -> str:
        return str(self.kite.place_order(
            variety="regular", exchange=exchange, tradingsymbol=tradingsymbol,
            transaction_type="SELL", quantity=int(qty), product=product,
            order_type="MARKET", validity="DAY", tag=tag, market_protection=market_protection))

    def modify_price(self, order_id, price, variety="regular") -> None:
        self.kite.modify_order(variety=variety, order_id=order_id, price=price, order_type="LIMIT")

    def cancel(self, order_id, variety="regular") -> None:
        self.kite.cancel_order(variety=variety, order_id=order_id)


class DryRunBroker:
    """Reads go to the real broker; writes are logged and simulated.

    Simulated SELLs fill immediately at the limit price (or LTP for the MARKET
    fallback) and reduce the quantity the positions read reports, so the exit sweep
    sees the book go flat exactly as it would live. Cancels and modifies of real
    orders are logged, never sent: in dry-run the TA must not touch the real book.
    """

    def __init__(self, real: Broker, on_action=None, clock=time.time):
        self.real = real
        self.on_action = on_action or (lambda msg: log.info("[DRY RUN] %s", msg))
        self.clock = clock
        self._lock = threading.Lock()
        self._n = 0
        self._orders: dict[str, dict] = {}
        self._sold: dict[tuple[str, str, str], int] = {}   # (exchange, symbol, product) -> qty sold today in dry-run

    def reset_day(self) -> None:
        with self._lock:
            self._orders.clear()
            self._sold.clear()

    # ---- reads
    def positions_net(self) -> list[dict]:
        rows = []
        with self._lock:
            sold = dict(self._sold)
        for p in self.real.positions_net():
            k = (p.get("exchange"), p.get("tradingsymbol"), p.get("product"))
            q = int(p.get("quantity") or 0)
            if q > 0:
                q = max(0, q - sold.get(k, 0))   # a manual sell after a dry-run sell must not go negative
            row = dict(p)
            row["quantity"] = q
            rows.append(row)
        return rows

    def orders(self) -> list[dict]:
        with self._lock:
            mine = [dict(o) for o in self._orders.values()]
        return self.real.orders() + mine

    def trades(self) -> list[dict]:
        return self.real.trades()

    def ltp(self, keys):
        return self.real.ltp(keys)

    def best_bid(self, keys):
        return self.real.best_bid(keys)

    def margins_equity(self):
        return self.real.margins_equity()

    def instruments(self, exchange):
        return self.real.instruments(exchange)

    # ---- writes (simulated)
    def _record(self, exchange, tradingsymbol, product, qty, price, order_type, tag) -> str:
        with self._lock:
            self._n += 1
            oid = f"DRY{self._n:04d}"
            self._orders[oid] = {
                "order_id": oid, "exchange": exchange, "tradingsymbol": tradingsymbol, "product": product,
                "transaction_type": "SELL", "quantity": qty, "filled_quantity": qty, "pending_quantity": 0,
                "average_price": price, "price": price, "order_type": order_type, "status": "COMPLETE",
                "tag": tag, "variety": "regular", "placed_at": self.clock(),
            }
            k = (exchange, tradingsymbol, product)
            self._sold[k] = self._sold.get(k, 0) + qty
        self.on_action(f"would SELL {qty} {tradingsymbol} {order_type} @ {price} tag={tag} -> {oid}")
        return oid

    def place_limit_sell(self, exchange, tradingsymbol, product, qty, price, tag) -> str:
        return self._record(exchange, tradingsymbol, product, int(qty), price, "LIMIT", tag)

    def place_market_sell(self, exchange, tradingsymbol, product, qty, tag, market_protection) -> str:
        price = self.real.ltp([f"{exchange}:{tradingsymbol}"]).get(f"{exchange}:{tradingsymbol}", 0.0)
        return self._record(exchange, tradingsymbol, product, int(qty), price, "MARKET", tag)

    def modify_price(self, order_id, price, variety="regular") -> None:
        self.on_action(f"would modify {order_id} to {price}")

    def cancel(self, order_id, variety="regular") -> None:
        self.on_action(f"would cancel {order_id}")
