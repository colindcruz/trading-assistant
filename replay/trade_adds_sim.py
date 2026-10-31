"""
Per-trade simulation WITH adds: how should the stop move when you add to a position?

    python replay/trade_adds_sim.py tradebook.xlsx archive_dir [2026-10-01 ...] [--pyramid-only]

Each flat-to-flat cycle of a contract is one trade. Every real BUY order in it (orders within
5 s merged, since Kite slices orders above the freeze limit) is replayed at
its own time, quantity and average fill price; the real sells are ignored, and the position is
closed by the v1 ladder (stop 1R, breakeven at +1R, from +1.5R trail 1R below the high) or at
15:06. A BUY that arrives after the simulated position has already stopped out is skipped and
counted. 1R = 20 NIFTY / 50 SENSEX points.

Policies for an add (the stop never moves down in any of them):
  first     adds ignored: the first order only (trade_rules_sim.py --rules=v1)
  ratchet   stop = max(stop, new average - 1R); stages measured from the first fill
  fixed     stop unchanged by the add; stages measured from the first fill
  reset     stop = max(stop, new average - 1R); stages re-measured from the new average
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from replay_october import read_tradebook  # noqa: E402
from trade_rules_sim import EXIT_ALL, RATES, load_candles  # noqa: E402
from ta.clock import rupees  # noqa: E402
from ta.ledger import Fill, charges_for_fill  # noqa: E402

R = {"NFO": 20.0, "BFO": 50.0}
POLICIES = ("first", "ratchet", "fixed", "reset")
RISK_CAP = 50_000
MERGE_S = 5            # seconds
PYRAMID_ONLY = False   # --pyramid-only: drop average-down adds (the rulebook forbids them)


def cycles(fills):
    """Flat-to-flat cycles per contract; each with its BUY orders (time, qty, avg price) and actual P&L."""
    pos, avg, booked = defaultdict(int), defaultdict(float), defaultdict(float)
    open_, out = {}, []
    for f in fills:
        s = f.tradingsymbol
        if f.side == "BUY":
            if pos[s] == 0:
                open_[s] = {"symbol": s, "exchange": f.exchange, "orders": {}, "order_list": []}
                booked[s] = 0.0
            t = open_[s]
            o = t["orders"].get(f.order_id)
            if o is None:
                o = t["orders"][f.order_id] = {"ts": f.ts, "qty": 0, "value": 0.0}
                t["order_list"].append(o)
            o["qty"] += f.quantity
            o["value"] += f.price * f.quantity
            avg[s] = (avg[s] * pos[s] + f.price * f.quantity) / (pos[s] + f.quantity)
            pos[s] += f.quantity
        elif pos[s] > 0:
            q = min(f.quantity, pos[s])
            booked[s] += (f.price - avg[s]) * q
            pos[s] -= q
            if pos[s] == 0 and s in open_:
                t = open_.pop(s)
                t["actual"] = booked[s]
                out.append(t)
    for s, t in open_.items():
        t["actual"] = booked[s]
        out.append(t)
    for t in out:
        # Kite slices an order above the freeze limit into several; orders within MERGE_S are one decision
        merged = []
        for o in t["order_list"]:
            if merged and (o["ts"] - merged[-1]["last_ts"]).total_seconds() <= MERGE_S:
                merged[-1]["qty"] += o["qty"]
                merged[-1]["value"] += o["value"]
                merged[-1]["last_ts"] = o["ts"]
            else:
                merged.append({**o, "last_ts": o["ts"]})
        for o in merged:
            o["price"] = o["value"] / o["qty"]
        t["order_list"] = merged
    return sorted(out, key=lambda t: t["order_list"][0]["ts"])


def charges(exchange, symbol, buys, exit_px, qty):
    ts = buys[0]["ts"]
    total = sum(charges_for_fill(Fill("b", "b", exchange, symbol, "MIS", "BUY", b["qty"], b["price"], ts), RATES)
                for b in buys)
    total += charges_for_fill(Fill("s", "s", exchange, symbol, "MIS", "SELL", qty, exit_px, ts), RATES)
    return total + (len(buys) + 1) * RATES.brokerage_per_order * (1 + RATES.gst_pct / 100)


def simulate(trade, candles, policy):
    r = R[trade["exchange"]]
    orders = trade["order_list"] if policy != "first" else trade["order_list"][:1]
    first = orders[0]
    anchor = first["price"]                       # where the stages are measured from
    qty, cost = first["qty"], first["value"]
    stop, high = anchor - r, anchor
    pending = list(orders[1:])
    applied, dropped, max_risk, breaches = [], [], qty * r, 0
    exit_all = first["ts"].replace(hour=EXIT_ALL[0], minute=EXIT_ALL[1], second=0, microsecond=0)
    exit_px = reason = None
    for c in candles:
        if c["t"] <= first["ts"]:
            continue
        # adds that filled before this candle
        while pending and pending[0]["ts"] < c["t"]:
            o = pending.pop(0)
            kind = "pyramid" if o["price"] > cost / qty else "avgdown"
            if kind == "avgdown" and PYRAMID_ONLY:
                dropped.append(o)
                continue
            qty += o["qty"]
            cost += o["value"]
            avg = cost / qty
            if policy in ("ratchet", "reset"):
                stop = max(stop, avg - r)
            if policy == "reset":
                anchor, high = avg, max(avg, o["price"])
            risk = qty * (avg - stop)
            max_risk = max(max_risk, risk)
            breaches += risk > RISK_CAP
            applied.append(kind)
        if c["t"] >= exit_all:
            exit_px, reason = c["o"], "EOD"
            break
        if c["l"] <= stop:
            exit_px, reason = (c["o"] if c["o"] < stop else stop), "STOP"
            break
        high = max(high, c["h"])
        gain = high - anchor
        if gain >= 1.5 * r:
            stop = max(stop, high - r)
        elif gain >= r:
            stop = max(stop, anchor)
    if exit_px is None:
        exit_px, reason = candles[-1]["c"], "OPEN"
    skipped = len(pending)
    gross = exit_px * qty - cost
    bought = [o for o in orders[:len(orders) - skipped] if not any(o is d for d in dropped)]
    net = gross - charges(trade["exchange"], trade["symbol"], bought, exit_px, qty)
    return {"gross": gross, "net": net, "reason": reason, "applied": applied, "skipped": skipped,
            "max_risk": max_risk, "breaches": breaches}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    global PYRAMID_ONLY
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    PYRAMID_ONLY = "--pyramid-only" in sys.argv
    tb, archive = Path(args[0]), Path(args[1])
    fills = read_tradebook(tb)
    days = args[2:] or sorted({f.ts.date().isoformat() for f in fills})
    tot = {p: defaultdict(float) for p in POLICIES}
    with_adds = {p: 0.0 for p in POLICIES}
    halves = {p: defaultdict(float) for p in POLICIES}
    n_trades = n_with_adds = n_pyr = n_avgdown = n_skipped = 0
    rows = []
    for day in days:
        candles = load_candles(archive, day)
        if not candles:
            print(f"{day}: no candle archive; skipped")
            continue
        for t in cycles([f for f in fills if f.ts.date().isoformat() == day]):
            cs = candles.get(t["symbol"])
            if not cs:
                continue
            n_trades += 1
            res = {p: simulate(t, cs, p) for p in POLICIES}
            half = "1-15" if int(day[-2:]) <= 15 else "16-31"
            for p in POLICIES:
                tot[p]["net"] += res[p]["net"]
                tot[p]["breaches"] += res[p]["breaches"]
                halves[p][half] += res[p]["net"]
            if res["fixed"]["applied"]:
                n_with_adds += 1
                n_pyr += res["fixed"]["applied"].count("pyramid")
                n_avgdown += res["fixed"]["applied"].count("avgdown")
                for p in POLICIES:
                    with_adds[p] += res[p]["net"]
                rows.append((day, t, res))
            n_skipped += res["fixed"]["skipped"]
            tot["actual"] = tot.get("actual", 0.0) + t["actual"]

    print(f"{'day':<11}{'contract':<22}{'adds':>9}" + "".join(f"{p:>11}" for p in POLICIES)
          + f"{'risk fixed':>12}{'risk ratch':>12}")
    for day, t, res in rows:
        a = res["fixed"]["applied"]
        tag = f"{a.count('pyramid')}p/{a.count('avgdown')}d"
        print(f"{day:<11}{t['symbol']:<22}{tag:>9}" + "".join(f"{rupees(res[p]['net']):>11}" for p in POLICIES)
              + f"{rupees(res['fixed']['max_risk']):>12}{rupees(res['ratchet']['max_risk']):>12}")
    print(f"\n{n_trades} trades with candles; {n_with_adds} had adds while the simulated position was open "
          f"({n_pyr} pyramid adds, {n_avgdown} average-down adds); {n_skipped} adds came after a simulated exit and were skipped")
    print(f"Actual (gross, all trades): {rupees(tot['actual'])}")
    print(f"{'policy':<10}{'net, all':>12}{'net, trades w/ adds':>22}{'Oct 1-15':>12}{'Oct 16-31':>12}{'>₹50k risk':>12}")
    for p in POLICIES:
        print(f"{p:<10}{rupees(tot[p]['net']):>12}{rupees(with_adds[p]):>22}{rupees(halves[p]['1-15']):>12}"
              f"{rupees(halves[p]['16-31']):>12}{int(tot[p]['breaches']):>12}")


if __name__ == "__main__":
    main()
