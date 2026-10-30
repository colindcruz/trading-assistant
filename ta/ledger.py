"""
The fill ledger: the Trading Assistant's own record of today's fills, from which it
derives open quantity, booked P&L, turnover, charges and the trade counter.

Why not the positions API? Its `pnl` is gross of charges, its `last_price` can be
stale, and `realised` has been seen reading 0 after round trips (Kite forum). Zerodha's
own advice is to compute P&L from fills and a live LTP. Positions are still read every
2 s as the source of truth for WHAT is open (see service.py); this ledger is the
source of truth for how much it has made or lost.

Pure: no I/O. Fills arrive from websocket order updates (COMPLETE, or partial fills
via filled_quantity deltas) and from the trades API on restart; both are deduplicated.

Trade definition (spec): a trade starts when a contract goes from no position to a
position and ends when it is back to no position. Adds and partial exits belong to
the same trade.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .config import ChargeRates


@dataclass(frozen=True)
class Fill:
    fill_id: str                 # trade_id from Kite, or "<order_id>:<cum_filled>" for socket deltas
    order_id: str
    exchange: str                # NFO / BFO
    tradingsymbol: str
    product: str                 # MIS / NRML
    side: str                    # BUY / SELL
    quantity: int
    price: float
    ts: datetime

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.tradingsymbol}:{self.product}"

    @property
    def value(self) -> float:
        return self.quantity * self.price


@dataclass
class ContractState:
    key: str
    exchange: str
    tradingsymbol: str
    product: str
    qty: int = 0                 # signed; Phase 1 expects >= 0
    avg_cost: float = 0.0        # average price of the open quantity
    booked: float = 0.0          # realised P&L, gross
    buy_value: float = 0.0
    sell_value: float = 0.0
    trade_no: int | None = None  # the trade number this contract's open position belongs to

    @property
    def instrument_key(self) -> str:
        """'EX:SYMBOL' — the key live prices are stored under (product-independent)."""
        return f"{self.exchange}:{self.tradingsymbol}"


@dataclass
class Trade:
    number: int
    key: str
    opened_at: datetime
    closed_at: datetime | None = None
    booked: float = 0.0


@dataclass
class LedgerEvent:
    kind: str                    # TRADE_OPENED | TRADE_CLOSED | POSITION_INCREASED | POSITION_REDUCED
    key: str
    trade_no: int
    fill: Fill


def charges_for_fill(fill: Fill, rates: ChargeRates) -> float:
    """Statutory and exchange charges for one fill, excluding brokerage (which is per
    order and added once per order_id by the ledger)."""
    value = fill.value
    exch_pct = rates.exch_bfo_pct if fill.exchange == "BFO" else rates.exch_nfo_pct
    exchange_charge = value * exch_pct / 100
    sebi = value / 1e7 * rates.sebi_per_crore
    gst = (exchange_charge + sebi) * rates.gst_pct / 100
    stt = value * rates.stt_sell_pct / 100 if fill.side == "SELL" else 0.0
    stamp = value * rates.stamp_buy_pct / 100 if fill.side == "BUY" else 0.0
    return exchange_charge + sebi + gst + stt + stamp


@dataclass
class Ledger:
    rates: ChargeRates
    contracts: dict[str, ContractState] = field(default_factory=dict)
    trades: list[Trade] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    seen_fill_ids: set[str] = field(default_factory=set)
    orders_seen: set[str] = field(default_factory=set)
    charges: float = 0.0
    brokerage: float = 0.0

    # ------------------------------------------------------------ derived
    @property
    def booked(self) -> float:
        return sum(c.booked for c in self.contracts.values())

    @property
    def turnover(self) -> float:
        return sum(c.buy_value + c.sell_value for c in self.contracts.values())

    @property
    def trades_opened(self) -> int:
        return len(self.trades)

    @property
    def trades_closed(self) -> int:
        return sum(1 for t in self.trades if t.closed_at is not None)

    @property
    def total_charges(self) -> float:
        """Charges plus brokerage plus GST on brokerage."""
        return self.charges + self.brokerage * (1 + self.rates.gst_pct / 100)

    def open_contracts(self) -> list[ContractState]:
        return [c for c in self.contracts.values() if c.qty != 0]

    def open_pnl(self, ltp_by_key: dict[str, float]) -> float:
        """`ltp_by_key` is keyed 'EX:SYMBOL' (no product), as the ticker delivers it."""
        total = 0.0
        for c in self.open_contracts():
            ltp = ltp_by_key.get(c.instrument_key)
            if ltp is None:
                ltp = c.avg_cost      # unknown price: mark at cost (spec: last fill price in replays)
            total += c.qty * (ltp - c.avg_cost)
        return total

    def net_pnl(self, ltp_by_key: dict[str, float]) -> float:
        return self.booked + self.open_pnl(ltp_by_key) - self.total_charges

    def breakdown(self, ltp_by_key: dict[str, float]) -> dict:
        open_ = self.open_pnl(ltp_by_key)
        return {
            "booked": self.booked,
            "open": open_,
            "gross": self.booked + open_,
            "charges": self.total_charges,
            "net": self.booked + open_ - self.total_charges,
            "turnover": self.turnover,
            "trades_opened": self.trades_opened,
            "trades_closed": self.trades_closed,
        }

    def flat_charge_estimate(self) -> float:
        """What the spec's fallback flat rate would say, for the EOD comparison."""
        return self.turnover * self.rates.flat_rate_pct / 100

    # ------------------------------------------------------------ mutation
    def apply(self, fill: Fill) -> list[LedgerEvent]:
        """Apply one fill. Returns the trade-level events it caused. Idempotent on fill_id."""
        if fill.fill_id in self.seen_fill_ids or fill.quantity <= 0:
            return []
        self.seen_fill_ids.add(fill.fill_id)
        self.fills.append(fill)

        self.charges += charges_for_fill(fill, self.rates)
        if fill.order_id not in self.orders_seen:
            self.orders_seen.add(fill.order_id)
            self.brokerage += self.rates.brokerage_per_order

        c = self.contracts.get(fill.key)
        if c is None:
            c = ContractState(fill.key, fill.exchange, fill.tradingsymbol, fill.product)
            self.contracts[fill.key] = c

        events: list[LedgerEvent] = []
        signed = fill.quantity if fill.side == "BUY" else -fill.quantity
        if fill.side == "BUY":
            c.buy_value += fill.value
        else:
            c.sell_value += fill.value

        was_flat = c.qty == 0
        same_direction = c.qty == 0 or (c.qty > 0) == (signed > 0)

        if was_flat:
            trade = Trade(number=len(self.trades) + 1, key=c.key, opened_at=fill.ts)
            self.trades.append(trade)
            c.trade_no = trade.number
            c.qty = signed
            c.avg_cost = fill.price
            events.append(LedgerEvent("TRADE_OPENED", c.key, trade.number, fill))
            return events

        if same_direction:
            total_cost = c.avg_cost * abs(c.qty) + fill.price * fill.quantity
            c.qty += signed
            c.avg_cost = total_cost / abs(c.qty)
            events.append(LedgerEvent("POSITION_INCREASED", c.key, c.trade_no or 0, fill))
            return events

        # Opposite direction: close some or all, possibly flip.
        closing = min(abs(c.qty), fill.quantity)
        direction = 1 if c.qty > 0 else -1
        realised = direction * (fill.price - c.avg_cost) * closing
        c.booked += realised
        trade = self.trades[c.trade_no - 1] if c.trade_no else None
        if trade is not None:
            trade.booked += realised
        c.qty += signed
        if c.qty == 0:
            if trade is not None:
                trade.closed_at = fill.ts
            closed_no = c.trade_no or 0
            c.trade_no = None
            c.avg_cost = 0.0
            events.append(LedgerEvent("TRADE_CLOSED", c.key, closed_no, fill))
        elif (c.qty > 0) != (direction > 0):
            # Flipped through zero: the old trade closed, a new one opened with the remainder.
            if trade is not None:
                trade.closed_at = fill.ts
            events.append(LedgerEvent("TRADE_CLOSED", c.key, c.trade_no or 0, fill))
            new_trade = Trade(number=len(self.trades) + 1, key=c.key, opened_at=fill.ts)
            self.trades.append(new_trade)
            c.trade_no = new_trade.number
            c.avg_cost = fill.price
            events.append(LedgerEvent("TRADE_OPENED", c.key, new_trade.number, fill))
        else:
            events.append(LedgerEvent("POSITION_REDUCED", c.key, c.trade_no or 0, fill))
        return events

    def apply_many(self, fills: list[Fill]) -> list[LedgerEvent]:
        events = []
        for f in sorted(fills, key=lambda x: (x.ts, x.fill_id)):
            events.extend(self.apply(f))
        return events

    def quantities(self) -> dict[str, int]:
        return {c.key: c.qty for c in self.contracts.values() if c.qty != 0}


# ------------------------------------------------------------ Kite adapters

def fill_from_trade(t: dict) -> Fill:
    """kite.trades() row -> Fill."""
    ts = t.get("fill_timestamp") or t.get("exchange_timestamp") or t.get("order_timestamp")
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    return Fill(
        fill_id=str(t["trade_id"]),
        order_id=str(t["order_id"]),
        exchange=t["exchange"],
        tradingsymbol=t["tradingsymbol"],
        product=t["product"],
        side=t["transaction_type"],
        quantity=int(t["quantity"]),
        price=float(t["average_price"]),
        ts=ts,
    )


def fill_from_order_update(data: dict, previously_filled: int, now: datetime) -> Fill | None:
    """A websocket order update -> the Fill for the quantity filled SINCE the last update
    we saw for this order (None if nothing new). Partial fills carry a cumulative
    filled_quantity; the ledger needs only the delta. Uses the order's average price for
    the delta, which is exact for a single fill and close enough for multi-fill deltas
    (the trades API reconciliation on restart is exact)."""
    filled = int(data.get("filled_quantity") or 0)
    delta = filled - previously_filled
    if delta <= 0:
        return None
    ts = data.get("exchange_update_timestamp") or data.get("order_timestamp") or now
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            ts = now
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=now.tzinfo)
    return Fill(
        fill_id=f"{data.get('order_id')}:{filled}",
        order_id=str(data.get("order_id")),
        exchange=data.get("exchange", ""),
        tradingsymbol=data.get("tradingsymbol", ""),
        product=data.get("product", ""),
        side=data.get("transaction_type", ""),
        quantity=delta,
        price=float(data.get("average_price") or 0),
        ts=ts,
    )
