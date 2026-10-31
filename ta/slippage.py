"""
Slippage measurement for the dry-run: how far each fill landed from the price when the
order went out. Pure; service.py supplies the reference prices.

Spec open item (2026-10-31): the October replays assume 2 NIFTY / 5 SENSEX points per
fill and the result turns on that number, so the TA measures it. Points are signed so
that a positive number is a cost: a BUY above its reference, a SELL below it.

Reference kinds:
  bid_at_send  the TA's own exit orders: best bid read just before the order was placed
  ltp          any other order: the last tick for that contract when Kite first reported
               the order; `ref_age_s` says how old that tick was
"""

from __future__ import annotations

from collections import defaultdict

from .clock import rupees


def slip_points(side: str, fill_price: float, ref_price: float) -> float:
    """Positive = cost. BUY above the reference or SELL below it."""
    return fill_price - ref_price if side == "BUY" else ref_price - fill_price


def record(order_id: str, exchange: str, symbol: str, side: str, qty: int, fill_price: float,
           ref_price: float | None, ref_kind: str, ref_age_s: float | None = None) -> dict:
    """One fill's slippage entry for the day record. `ref_price` None = no reference known."""
    pts = None if not ref_price else round(slip_points(side, fill_price, ref_price), 2)
    return {"order_id": order_id, "exchange": exchange, "symbol": symbol, "side": side, "qty": qty,
            "fill": fill_price, "ref": ref_price, "ref_kind": ref_kind if ref_price else "none",
            "ref_age_s": None if ref_age_s is None else round(ref_age_s, 1),
            "points": pts, "rupees": None if pts is None else round(pts * qty, 2)}


def summary(records: list[dict]) -> str:
    """One line per exchange: fills measured, quantity-weighted average points, total rupees;
    plus how many fills had no reference."""
    if not records:
        return "Slippage: no fills measured"
    by_ex: dict[str, list[dict]] = defaultdict(list)
    missing = 0
    for r in records:
        if r["points"] is None:
            missing += 1
        else:
            by_ex[r["exchange"]].append(r)
    parts = []
    for ex in sorted(by_ex):
        rs = by_ex[ex]
        qty = sum(r["qty"] for r in rs)
        cost = sum(r["rupees"] for r in rs)
        name = {"NFO": "NIFTY", "BFO": "SENSEX"}.get(ex, ex)
        parts.append(f"{name} {len(rs)} fills, avg {cost / qty:+.2f} pts, {rupees(-cost)}")
    line = "Slippage: " + ("; ".join(parts) if parts else "none measured")
    if missing:
        line += f" ({missing} fills had no reference price)"
    return line
