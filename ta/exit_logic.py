"""
Pure helpers over Kite-shaped dicts. No Kite session, no I/O.

Copied on 2026-10-31 from C:/Users/colin/projects/trading-dashboard/exit_logic.py
(the quick-exit dashboard, in production on the droplet since 2026-09-11), trimmed to
what the Trading Assistant needs. The dashboard's short-side and hedge helpers are
left out: Phase 1 only ever SELLS bought options.
"""

from __future__ import annotations

import math
from datetime import date, datetime

# Only these two indices are in scope. Matched against the instruments dump's
# `name` column exactly, which keeps NIFTYNXT50 and single-stock options out.
INDEX_EXCHANGE = {"NIFTY": "NFO", "SENSEX": "BFO"}

# Order statuses after which an order can no longer change.
TERMINAL_STATUSES = {"COMPLETE", "CANCELLED", "REJECTED"}
OPEN_STATUSES = {"OPEN", "TRIGGER PENDING", "OPEN PENDING", "MODIFY PENDING", "VALIDATION PENDING", "PUT ORDER REQ RECEIVED"}

LONG, SHORT = "LONG", "SHORT"


def build_instrument_index(rows: list[dict]) -> dict[tuple[str, str], dict]:
    """(exchange, tradingsymbol) -> fields we need, for NIFTY/SENSEX CE/PE rows only.
    `rows` is kite.instruments("NFO") + kite.instruments("BFO")."""
    index = {}
    for i in rows:
        name = i.get("name")
        if INDEX_EXCHANGE.get(name) != i.get("exchange"):
            continue
        if i.get("instrument_type") not in ("CE", "PE"):
            continue
        expiry = i.get("expiry")
        if isinstance(expiry, datetime):
            expiry = expiry.date()
        index[(i["exchange"], i["tradingsymbol"])] = {
            "index": name,
            "instrument_token": int(i["instrument_token"]),
            "lot_size": int(i["lot_size"]),
            "tick_size": float(i["tick_size"]),
            "strike": float(i["strike"]),
            "option_type": i["instrument_type"],
            "expiry": expiry.isoformat() if isinstance(expiry, date) else (expiry or None),
        }
    return index


def row_key(exchange: str, tradingsymbol: str, product: str) -> str:
    """The same symbol held as MIS and NRML is two net positions; each is its own row."""
    return f"{exchange}:{tradingsymbol}:{product}"


def select_index_options(net: list[dict], instruments: dict) -> tuple[list[dict], list[str]]:
    """Filters kite.positions()["net"] to open NIFTY/SENSEX option rows (long and short).
    Each row carries `side` and an absolute `quantity`. Returns (rows, unknown_symbols):
    unknown = open NFO/BFO options missing from the instruments index (stock options,
    other indices, or a contract listed after the dump was fetched)."""
    rows, unknown = [], []
    for p in net:
        qty = int(p.get("quantity") or 0)
        exchange = p.get("exchange")
        symbol = p.get("tradingsymbol", "")
        if qty == 0 or exchange not in ("NFO", "BFO"):
            continue
        meta = instruments.get((exchange, symbol))
        if meta is None:
            if symbol.endswith(("CE", "PE")):
                unknown.append(symbol)
            continue
        rows.append({
            "key": row_key(exchange, symbol, p["product"]),
            "exchange": exchange,
            "tradingsymbol": symbol,
            "product": p["product"],
            "side": LONG if qty > 0 else SHORT,
            "quantity": abs(qty),
            "average_price": float(p.get("average_price") or 0),
            "last_price": float(p.get("last_price") or 0),
            **meta,
        })
    return rows, unknown


def split_for_freeze(qty: int, lot_size: int, freeze_qty: int) -> list[int]:
    """Splits an order into exchange-freeze-sized chunks, each a whole number of lots
    (the final chunk may be a stub if qty itself isn't a lot multiple)."""
    lot_size = max(1, int(lot_size))
    max_chunk = (freeze_qty // lot_size) * lot_size or lot_size
    chunks = []
    remaining = qty
    while remaining > 0:
        chunk = min(remaining, max_chunk)
        chunks.append(chunk)
        remaining -= chunk
    return chunks


def round_to_tick(price: float, tick_size: float = 0.05) -> float:
    """Nearest valid price. round(x, 2) does NOT guarantee a multiple of 0.05 (218.64):
    Zerodha rejects that outright ("Order price is not a multiple of tick size"), hit for
    real on 2026-08-24 on every stop-triggered exit until it was caught."""
    tick = tick_size if tick_size and tick_size > 0 else 0.05
    ticks = math.floor(price / tick + 0.5)
    return round(ticks * tick, 2)


def lpp_floor(reference: float) -> float:
    """Lowest limit price the exchange accepts for a sell (Limit Price Protection band):
    40% below the reference for premiums above Rs 50, Rs 20 below it otherwise.
    Confirmed 2026-10-30 from NSE / Zerodha support pages."""
    if reference > 50:
        return reference * 0.6
    return max(0.05, reference - 20)


def same_contract(order: dict, row: dict) -> bool:
    return (order.get("exchange") == row["exchange"]
            and order.get("tradingsymbol") == row["tradingsymbol"]
            and order.get("product") == row["product"])


def open_qty(order: dict) -> int:
    """Quantity of an order still waiting to fill."""
    pending = order.get("pending_quantity")
    if pending is None:
        pending = int(order.get("quantity") or 0) - int(order.get("filled_quantity") or 0)
    return max(0, int(pending))


def is_open_order(order: dict) -> bool:
    return order.get("status") not in TERMINAL_STATUSES


def pending_sell_qty(orders: list[dict], row: dict, tag_prefix: str) -> int:
    """Unfilled quantity resting in OUR open SELL orders on one row. Only orders whose
    tag starts with our prefix count, so another system's order never blocks an exit."""
    return sum(open_qty(o) for o in orders
               if str(o.get("tag") or "").startswith(tag_prefix) and is_open_order(o)
               and o.get("transaction_type") == "SELL" and same_contract(o, row))
