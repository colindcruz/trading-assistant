"""Shared test doubles: settings, a scripted FakeBroker, a controllable clock, a fake Telegram."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ta.clock import IST
from ta.config import ChargeRates, RulePercents, Settings, Thresholds


def make_settings(state_dir: Path | str = ".", **over) -> Settings:
    base = dict(
        kite_api_key="key", access_token_path=Path(".access_token"),
        telegram_bot_token="bot", telegram_chat_id="100", telegram_user_id="7", ntfy_topic="",
        percents=RulePercents(), charges=ChargeRates(), capital_sanity_pct=50.0, capital_override=0.0,
        exit_buffer_pct=5.0, reprice_attempts=3, reprice_interval_seconds=2.0, urgent_after_seconds=30.0,
        order_gap_seconds=0.2, market_protection=-1, skip_below_price=0.0,
        freeze_qty={"NIFTY": 3510, "SENSEX": 1000}, cancel_wait_seconds=3.0,
        pre_open=(9, 15), market_open=(9, 15), exit_all_time=(15, 6), no_new_time=(15, 10),
        summary_time=(15, 30), market_close=(15, 40), guardian_check_time=(9, 0),
        state_dir=Path(state_dir), log_file=Path(state_dir) / "test.log",
        heartbeat_timeout_seconds=30.0, positions_poll_seconds=2.0, token_check_seconds=30.0, telegram_poll_timeout=1,
    )
    base.update(over)
    return Settings(**base)


def thresholds(capital: float = 20_00_000) -> Thresholds:
    return Thresholds.from_capital(capital, RulePercents())


def ist(y, mo, d, h, mi, s=0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=IST)


# Two real-looking contracts
NIFTY = {"name": "NIFTY", "exchange": "NFO", "tradingsymbol": "NIFTY26N0525000CE", "instrument_type": "CE",
         "instrument_token": 1001, "lot_size": 65, "tick_size": 0.05, "strike": 25000, "expiry": "2026-11-05"}
NIFTY2 = {"name": "NIFTY", "exchange": "NFO", "tradingsymbol": "NIFTY26N0524800PE", "instrument_type": "PE",
          "instrument_token": 1002, "lot_size": 65, "tick_size": 0.05, "strike": 24800, "expiry": "2026-11-05"}
SENSEX = {"name": "SENSEX", "exchange": "BFO", "tradingsymbol": "SENSEX26N0682000CE", "instrument_type": "CE",
          "instrument_token": 2001, "lot_size": 20, "tick_size": 0.05, "strike": 82000, "expiry": "2026-11-06"}
STOCK = {"name": "RELIANCE", "exchange": "NFO", "tradingsymbol": "RELIANCE26NOV1500CE", "instrument_type": "CE",
         "instrument_token": 3001, "lot_size": 500, "tick_size": 0.05, "strike": 1500, "expiry": "2026-11-26"}
INSTRUMENT_ROWS = [NIFTY, NIFTY2, SENSEX, STOCK]


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class FakeBroker:
    """Scripted Kite. Positions are long rows; SELL orders fill according to `fill_mode`:
    'immediate' (COMPLETE on the next read, position reduced), 'never' (stay OPEN),
    'reject' (REJECTED), or a callable(order) -> mode."""

    def __init__(self, positions: list[dict] | None = None, fill_mode="immediate", bids: dict | None = None):
        self.positions = positions or []
        self.fill_mode = fill_mode
        self.bids = bids or {}
        self._orders: dict[str, dict] = {}
        self._n = 0
        self.placed: list[dict] = []
        self.modified: list[tuple[str, float]] = []
        self.cancelled: list[str] = []
        self.trades_rows: list[dict] = []
        self.margins = {"available": {"opening_balance": 20_00_000.0, "intraday_payin": 0.0}}
        self.raise_on_limit: Exception | None = None
        self.raise_on_market: Exception | None = None

    # ---- helpers for tests
    def add_position(self, inst: dict, qty: int, avg: float, product="MIS", last_price=None):
        self.positions.append({"exchange": inst["exchange"], "tradingsymbol": inst["tradingsymbol"], "product": product,
                               "quantity": qty, "average_price": avg, "last_price": last_price or avg,
                               "instrument_token": inst["instrument_token"]})

    def add_resting_order(self, inst: dict, order_type="SL", transaction="SELL", qty=65, tag=None, product="MIS"):
        self._n += 1
        oid = f"R{self._n}"
        self._orders[oid] = {"order_id": oid, "exchange": inst["exchange"], "tradingsymbol": inst["tradingsymbol"],
                             "product": product, "transaction_type": transaction, "order_type": order_type,
                             "quantity": qty, "filled_quantity": 0, "pending_quantity": qty, "status": "TRIGGER PENDING",
                             "tag": tag, "variety": "regular"}
        return oid

    def _settle(self):
        for o in self._orders.values():
            if o["status"] != "PLACED":
                continue
            mode = self.fill_mode(o) if callable(self.fill_mode) else self.fill_mode
            if mode == "immediate":
                o["status"], o["filled_quantity"], o["pending_quantity"] = "COMPLETE", o["quantity"], 0
                o["average_price"] = o.get("price") or o.get("ltp") or 0
                for p in self.positions:
                    if (p["exchange"], p["tradingsymbol"], p["product"]) == (o["exchange"], o["tradingsymbol"], o["product"]):
                        p["quantity"] -= o["quantity"]
            elif mode == "reject":
                o["status"], o["status_message"] = "REJECTED", "scripted rejection"
            else:
                o["status"] = "OPEN"

    # ---- Broker protocol
    def positions_net(self):
        self._settle()
        return [dict(p) for p in self.positions]

    def orders(self):
        self._settle()
        return [dict(o) for o in self._orders.values()]

    def trades(self):
        return list(self.trades_rows)

    def ltp(self, keys):
        out = {}
        for k in keys:
            ex, sym = k.split(":")
            for p in self.positions:
                if p["exchange"] == ex and p["tradingsymbol"] == sym:
                    out[k] = p["last_price"]
        return out

    def best_bid(self, keys):
        return {k: self.bids.get(k, self.ltp([k]).get(k, 0.0)) for k in keys}

    def margins_equity(self):
        return self.margins

    def instruments(self, exchange):
        return [dict(r) for r in INSTRUMENT_ROWS if r["exchange"] == exchange]

    def _new_order(self, exchange, tradingsymbol, product, qty, tag, order_type, price):
        self._n += 1
        oid = f"O{self._n}"
        ltp = self.ltp([f"{exchange}:{tradingsymbol}"]).get(f"{exchange}:{tradingsymbol}", 0)
        self._orders[oid] = {"order_id": oid, "exchange": exchange, "tradingsymbol": tradingsymbol, "product": product,
                             "transaction_type": "SELL", "order_type": order_type, "quantity": qty, "filled_quantity": 0,
                             "pending_quantity": qty, "status": "PLACED", "tag": tag, "price": price, "ltp": ltp,
                             "variety": "regular"}
        self.placed.append(dict(self._orders[oid]))
        return oid

    def place_limit_sell(self, exchange, tradingsymbol, product, qty, price, tag):
        if self.raise_on_limit:
            raise self.raise_on_limit
        return self._new_order(exchange, tradingsymbol, product, qty, tag, "LIMIT", price)

    def place_market_sell(self, exchange, tradingsymbol, product, qty, tag, market_protection):
        if self.raise_on_market:
            raise self.raise_on_market
        assert market_protection != 0
        return self._new_order(exchange, tradingsymbol, product, qty, tag, "MARKET", None)

    def modify_price(self, order_id, price, variety="regular"):
        self.modified.append((order_id, price))
        self._orders[order_id]["price"] = price

    def cancel(self, order_id, variety="regular"):
        self.cancelled.append(order_id)
        o = self._orders[order_id]
        o["status"], o["pending_quantity"] = "CANCELLED", 0


class FakeTelegram:
    def __init__(self):
        self.sent: list[str] = []
        self.ntfys: list[tuple[str, str, str]] = []
        self.queued: list[tuple[str, str, str]] = []   # (text, from_id, chat_id)

    def send(self, text):
        self.sent.append(text)
        return True

    def ntfy(self, title, body, priority="default"):
        self.ntfys.append((title, body, priority))

    def fetch_commands(self, offset, timeout=1):
        from ta.telegram import Command
        out = [Command(offset + i + 1, t, f, c) for i, (t, f, c) in enumerate(self.queued) if f == "7" and c == "100"]
        n = len(self.queued)
        self.queued = []
        return out, offset + n
