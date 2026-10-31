"""
Per-trade stops held by the TA (stop_mode = ta). Pure: no Kite, no Telegram, no clock.

Spec rulebook (decided 2026-10-31):
  stop        first fill - 1R; 1R = 20 NIFTY / 50 SENSEX points
  breakeven   stop to the first fill once the high is +1R above it
  trail       v1:   from +1.5R, 1R below the high
              wide: from +2R,   1.5R below the high
  adds        not allowed in v1; if one happens, stop = max(stop, new average - 1R).
              Stages stay measured from the first fill, so an add never resets them.
  the stop only ever moves up.

Both ladders run on every trade: the active one (settings) drives the alerts and the exit,
the other is recorded alongside so the dry-run can compare them. A trade's state is a
plain dict (JSON-safe) kept in DayState.stops, keyed by the ledger's contract key.

Prices come in as samples (the service passes the latest tick once a second), so a spike
between samples is missed: the high is understated, which keeps the trail lower, never
higher, than tick-by-tick.
"""

from __future__ import annotations

from dataclasses import dataclass

R_POINTS = {"NIFTY": 20.0, "SENSEX": 50.0}


@dataclass(frozen=True)
class Ladder:
    breakeven_at: float   # gain in R that moves the stop to the first fill
    trail_from: float     # gain in R from which the stop trails the high
    trail_dist: float     # distance below the high, in R


LADDERS = {"v1": Ladder(1.0, 1.5, 1.0), "wide": Ladder(1.0, 2.0, 1.5)}
MOVE_ALERT_R = 0.5        # alert when the active stop has risen this much since the last alert (10 / 25 points)


@dataclass
class StopEvent:
    kind: str             # STOP_ARMED | STOP_MOVED | ADD_DETECTED | STOP_HIT | SHADOW_HIT | STOP_CLEARED
    key: str
    message: str
    ladder: str = ""
    price: float | None = None
    urgent: bool = False


def _round(x: float) -> float:
    return round(x, 2)


def open_trade(stops: dict, key: str, symbol: str, index: str, qty: int, price: float,
               active: str, at: str) -> list[StopEvent]:
    """A contract went from flat to long: arm both ladders at first fill - 1R."""
    r = R_POINTS[index]
    stop = _round(price - r)
    stops[key] = {
        "symbol": symbol, "index": index, "r": r, "first": price, "qty": qty, "avg": price,
        "high": price, "opened_at": at, "adds": 0, "active": active, "peak_qty": qty,
        "ladders": {name: {"stop": stop, "stage": "initial", "hit_at": None, "hit_price": None}
                    for name in LADDERS},
        "last_alerted_stop": stop,
    }
    risk = qty * (price - stop)
    return [StopEvent("STOP_ARMED", key,
                      f"Stop armed: {symbol} x{qty} @ {price:g}, stop {stop:g} ({active}), risk ₹{risk:,.0f}",
                      ladder=active, price=stop)]


def add_to_trade(stops: dict, key: str, qty: int, avg: float) -> list[StopEvent]:
    """The position grew (an add). Ratchet every ladder to max(stop, new average - 1R)."""
    t = stops.get(key)
    if t is None:
        return []
    t["qty"], t["avg"], t["adds"] = qty, avg, t["adds"] + 1
    t["peak_qty"] = max(t.get("peak_qty", qty), qty)
    for lad in t["ladders"].values():
        if lad["hit_at"] is None:
            lad["stop"] = _round(max(lad["stop"], avg - t["r"]))
    stop = t["ladders"][t["active"]]["stop"]
    t["last_alerted_stop"] = stop
    risk = qty * (avg - stop)
    return [StopEvent("ADD_DETECTED", key,
                      f"Add detected (not allowed in v1): {t['symbol']} now x{qty} avg {avg:g}; "
                      f"stop ratcheted to {stop:g}, risk ₹{risk:,.0f}", ladder=t["active"], price=stop, urgent=True)]


def reduce_trade(stops: dict, key: str, qty: int) -> None:
    """Part of the position was sold: the stop covers what is left."""
    if key in stops:
        stops[key]["qty"] = qty


def close_trade(stops: dict, key: str) -> dict | None:
    """The contract is flat: the stop goes. Returns the trade's final state for the log."""
    return stops.pop(key, None)


def on_price(stops: dict, key: str, price: float, at: str) -> list[StopEvent]:
    """One price sample. Each live ladder: hit if price <= stop (checked first, against the
    stop as it stood), otherwise lift the high and ratchet. Events for the active ladder;
    a SHADOW_HIT for the other."""
    t = stops.get(key)
    if t is None or price <= 0:
        return []
    events: list[StopEvent] = []
    r, first = t["r"], t["first"]
    for name, lad in t["ladders"].items():
        if lad["hit_at"] is not None:
            continue
        if price <= lad["stop"]:
            lad["hit_at"], lad["hit_price"] = at, price
            pts = price - first
            if name == t["active"]:
                events.append(StopEvent("STOP_HIT", key,
                                        f"Stop hit: {t['symbol']} {price:g} <= {lad['stop']:g} ({lad['stage']}), "
                                        f"{pts:+.1f} pts from entry", ladder=name, price=price, urgent=True))
            else:
                events.append(StopEvent("SHADOW_HIT", key, f"{name} stop would exit {t['symbol']} at {price:g}",
                                        ladder=name, price=price))
    t["high"] = max(t["high"], price)
    gain = (t["high"] - first) / r
    for name, lad in t["ladders"].items():
        if lad["hit_at"] is not None:
            continue
        spec = LADDERS[name]
        before = lad["stop"]
        if gain >= spec.breakeven_at:
            lad["stop"] = max(lad["stop"], first)
            if lad["stage"] == "initial":
                lad["stage"] = "breakeven"
        if gain >= spec.trail_from:
            lad["stop"] = _round(max(lad["stop"], t["high"] - spec.trail_dist * r))
            lad["stage"] = "trail"
        if name == t["active"] and lad["stop"] > before:
            to_breakeven = before < first <= lad["stop"]
            if to_breakeven or lad["stop"] - t["last_alerted_stop"] >= MOVE_ALERT_R * r:
                events.append(StopEvent("STOP_MOVED", key,
                                        f"Stop moved: {t['symbol']} {t['last_alerted_stop']:g} -> {lad['stop']:g} "
                                        f"({lad['stage']}, high {t['high']:g})", ladder=name, price=lad["stop"]))
                t["last_alerted_stop"] = lad["stop"]
    return events


def active_stop(stops: dict, key: str) -> float | None:
    t = stops.get(key)
    return None if t is None else t["ladders"][t["active"]]["stop"]


def summary_row(key: str, t: dict, closed_at: str, exit_price: float | None) -> dict:
    """What the Saturday review compares: entry, each ladder's exit, and the actual exit."""
    return {"key": key, "symbol": t["symbol"], "opened_at": t["opened_at"], "closed_at": closed_at,
            "first": t["first"], "avg": t["avg"], "qty": t.get("peak_qty", t["qty"]), "adds": t["adds"],
            "high": t["high"], "actual_exit": exit_price,
            "ladders": {n: {"stage": l["stage"], "stop": l["stop"], "hit_at": l["hit_at"], "hit_price": l["hit_price"]}
                        for n, l in t["ladders"].items()}}


def compare(stop_log: list[dict]) -> str:
    """Evening line: what each ladder would have made on the day's closed trades against what
    you did. A ladder that never hit is taken to the actual exit. Gross, at the average price
    and the trade's largest quantity; partial exits are not modelled."""
    rows = [r for r in stop_log if r.get("actual_exit") is not None]
    if not rows:
        return "Stops: no closed trades"
    totals = {name: 0.0 for name in LADDERS}
    actual = 0.0
    for r in rows:
        actual += (r["actual_exit"] - r["avg"]) * r["qty"]
        for name, lad in r["ladders"].items():
            exit_px = lad["hit_price"] if lad["hit_price"] is not None else r["actual_exit"]
            totals[name] += (exit_px - r["avg"]) * r["qty"]
    parts = ", ".join(f"{n} ₹{v:,.0f}" for n, v in totals.items())
    return f"Stops on {len(rows)} trades (gross): {parts}; actual ₹{actual:,.0f}"


def status_lines(stops: dict) -> list[str]:
    out = []
    for t in stops.values():
        act = t["ladders"][t["active"]]
        other = [f"{n} {l['stop']:g}" for n, l in t["ladders"].items() if n != t["active"]]
        hit = " HIT" if act["hit_at"] else ""
        out.append(f"{t['symbol']} x{t['qty']} @ {t['avg']:g}: stop {act['stop']:g} ({act['stage']}{hit}); "
                   + ", ".join(other))
    return out
