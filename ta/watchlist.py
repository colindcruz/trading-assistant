"""
Near-the-money watchlist: which option contracts to keep subscribed so that the TA has a
live price for a contract BEFORE you buy it. Pure; service.py supplies the spot prices.

Why: slippage on an entry is the fill against the contract's price when the order was
sent (slippage.py), and the TA otherwise only subscribes to contracts already held, so
your first buy in a contract had no reference. Spec open item, 2026-10-31.
"""

from __future__ import annotations

from datetime import date

# Kite LTP keys for the underlying indices
SPOT_KEYS = {"NIFTY": "NSE:NIFTY 50", "SENSEX": "BSE:SENSEX"}


def near_money_tokens(instruments: dict, spot: dict[str, float], today: date,
                      strikes_each_side: int = 10, expiries: int = 2) -> set[int]:
    """Instrument tokens of the CE and PE contracts within `strikes_each_side` strikes of the
    strike nearest spot, on the `expiries` nearest expiries on or after today, per index.
    `instruments` is build_instrument_index() output; an index with no spot is skipped."""
    if strikes_each_side <= 0 or expiries <= 0:
        return set()
    by_index: dict[str, dict[str, list[tuple[float, int]]]] = {}
    for meta in instruments.values():
        exp = meta.get("expiry")
        if not exp or date.fromisoformat(exp) < today:
            continue
        by_index.setdefault(meta["index"], {}).setdefault(exp, []).append((meta["strike"], meta["instrument_token"]))
    tokens: set[int] = set()
    for name, by_expiry in by_index.items():
        s = spot.get(name)
        if not s:
            continue
        for exp in sorted(by_expiry)[:expiries]:
            contracts = by_expiry[exp]
            strikes = sorted({k for k, _ in contracts})
            atm = min(range(len(strikes)), key=lambda i: abs(strikes[i] - s))
            keep = set(strikes[max(0, atm - strikes_each_side): atm + strikes_each_side + 1])
            tokens.update(tok for k, tok in contracts if k in keep)
    return tokens
