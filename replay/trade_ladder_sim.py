"""
Compare exit ladders (trail + profit taking) on the INITIAL position only, at rulebook size.

    python replay/trade_ladder_sim.py tradebook.xlsx archive_dir [2026-10-16 ...]

A trade = the first entry of each flat-to-flat cycle (orders within 5 s merged), resized to
2,600 NIFTY / 1,000 SENSEX; adds are ignored. Distances are in R (1R = 20 NIFTY / 50 SENSEX
points). Each minute: the stop is checked against the low first (a gap fills at the open),
then targets fill at their price if the high reaches them, then the stop ratchets off the
high. Whatever is left is sold at 15:06. Because the trail only updates once per 1-minute
candle, tight trails look slightly better here than they would tick by tick.

SLIP sets points lost on every market fill; a ladder with maker=True books its targets as
resting limit orders (no slippage, filled only once the high trades a tick through).
run_day() replays one day as an account, with a daily loss limit and an optional trade cap.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from replay_october import read_tradebook  # noqa: E402
from trade_adds_sim import R, cycles  # noqa: E402
from trade_rules_sim import EXIT_ALL, RATES, load_candles  # noqa: E402
from ta.clock import rupees  # noqa: E402
from ta.ledger import Fill, charges_for_fill  # noqa: E402

SIZE = {"NFO": 2600, "BFO": 1000}
TICK = 0.05
SLIP = {"NFO": 0.0, "BFO": 0.0}   # points lost on every fill (the spec's replays use 2 NIFTY / 5 SENSEX)

# be: move the stop to entry at this gain; trail: [(from gain, distance)], the last matching wins;
# book: [(at gain, fraction of the initial size)]. All in R.
LADDERS = {
    "v1":              dict(be=1.0, trail=[(1.5, 1.0)], book=[]),
    "tight 0.5R":      dict(be=1.0, trail=[(1.5, 0.5)], book=[]),
    "stepped":         dict(be=1.0, trail=[(1.5, 1.0), (2.5, 0.75), (4.0, 0.5)], book=[]),
    "half@1R+v1":      dict(be=1.0, trail=[(1.5, 1.0)], book=[(1.0, 0.5)]),
    "half@2R+v1":      dict(be=1.0, trail=[(1.5, 1.0)], book=[(2.0, 0.5)]),
    "half@2R+stepped": dict(be=1.0, trail=[(1.5, 1.0), (2.5, 0.75), (4.0, 0.5)], book=[(2.0, 0.5)]),
    "thirds 1R/2R+v1": dict(be=1.0, trail=[(1.5, 1.0)], book=[(1.0, 1 / 3), (2.0, 1 / 3)]),
    "engine":          dict(be=None, trail=[(1.0, 0.4), (1.5, 0.3)], book=[(0.75, 0.5)], post_book=0.05),
}


def simulate(t, candles, lad, path=None):
    """Net P&L of the trade. If `path` is a list, (minute, realised gross, qty left, close) is appended
    for every minute the trade is open, the exit minute included."""
    r = R[t["exchange"]]
    o0 = t["order_list"][0]
    slip = SLIP[t["exchange"]]
    entry, size = o0["price"] + slip, SIZE[t["exchange"]]
    left, stop, high, pnl = size, entry - r, entry, 0.0
    exits = []
    todo = sorted(lad["book"])
    exit_all = o0["ts"].replace(hour=EXIT_ALL[0], minute=EXIT_ALL[1], second=0, microsecond=0)
    def mark(c):
        if path is not None:
            path.append((c["t"], sum((px - entry) * q for px, q in exits), left, c["c"]))

    for c in candles:
        if c["t"] <= o0["ts"]:
            continue
        if c["t"] >= exit_all or c["l"] <= stop:
            px = c["o"] if c["t"] >= exit_all else min(c["o"], stop)
            exits.append((px - slip, left))
            left = 0
            mark(c)
            break
        high = max(high, c["h"])
        gain = (high - entry) / r
        # a resting take-profit ("maker") fills at its price, no slippage, once the high trades a tick through it
        tick = TICK if lad.get("maker") else 0.0
        while todo and high - tick >= entry + todo[0][0] * r and left > 0:
            at, frac = todo.pop(0)
            q = min(left, round(size * frac))
            exits.append((entry + at * r - (0.0 if lad.get("maker") else slip), q))
            left -= q
            if "post_book" in lad:
                stop = max(stop, entry + lad["post_book"] * r)
        mark(c)
        if left == 0:
            break
        if lad["be"] is not None and gain >= lad["be"]:
            stop = max(stop, entry)
        for frm, dist in lad["trail"]:
            if gain >= frm:
                stop = max(stop, high - dist * r)
    else:
        exits.append((candles[-1]["c"] - slip, left))
        left = 0
        mark(candles[-1])
    pnl = sum((px - entry) * q for px, q in exits)
    ts = o0["ts"]
    ch = charges_for_fill(Fill("b", "b", t["exchange"], t["symbol"], "MIS", "BUY", size, entry, ts), RATES)
    ch += sum(charges_for_fill(Fill("s", "s", t["exchange"], t["symbol"], "MIS", "SELL", q, px, ts), RATES)
              for px, q in exits if q)
    ch += (1 + sum(1 for _, q in exits if q)) * RATES.brokerage_per_order * (1 + RATES.gst_pct / 100)
    return pnl - ch


def run_day(trades, lad, limit=None, cap=None):
    """One day as an account: trades = [(trade, candles)]. Each minute the day's net (closed trades,
    open trades marked at the close, every trade's charges counted from its entry) is checked against
    -limit; on a breach everything open is sold at that minute's close and no later entry is taken.
    cap = take only the first N entries. Returns (day net, breach minute or None)."""
    trades = sorted(trades, key=lambda x: x[0]["order_list"][0]["ts"])[:cap]
    legs = []
    for t, cs in trades:
        path = []
        net = simulate(t, cs, lad, path)
        if not path:
            continue
        gross = path[-1][1]
        legs.append({"entry_ts": t["order_list"][0]["ts"], "entry": t["order_list"][0]["price"] + SLIP[t["exchange"]],
                     "path": path, "net": net, "charges": gross - net})
    if limit is None:
        return sum(l["net"] for l in legs), None
    minutes = sorted({p[0] for l in legs for p in l["path"]})
    for m in minutes:
        total = 0.0
        for l in legs:
            if l["entry_ts"] >= m:
                continue
            if m > l["path"][-1][0]:
                total += l["net"]
                continue
            state = None
            for p in l["path"]:
                if p[0] > m:
                    break
                state = p
            if state is None:
                total -= l["charges"]
            else:
                _, realised, left, close = state
                total += realised + (close - l["entry"]) * left - l["charges"]
        if total <= -limit:
            return total, m
    return sum(l["net"] for l in legs), None


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    tb, archive = Path(sys.argv[1]), Path(sys.argv[2])
    fills = read_tradebook(tb)
    days = sys.argv[3:] or sorted({f.ts.date().isoformat() for f in fills})
    res = {k: defaultdict(float) for k in LADDERS}
    per_day = {k: defaultdict(float) for k in LADDERS}
    n = 0
    for day in days:
        candles = load_candles(archive, day)
        for t in cycles([f for f in fills if f.ts.date().isoformat() == day]):
            cs = candles.get(t["symbol"])
            if not cs:
                continue
            n += 1
            half = "1-15" if int(day[-2:]) <= 15 else "16-31"
            for k, lad in LADDERS.items():
                net = simulate(t, cs, lad)
                res[k][half] += net
                res[k]["all"] += net
                per_day[k][day] += net
                res[k]["wins"] += net > 0
    print(f"{n} trades, initial position at 2,600 NIFTY / 1,000 SENSEX, net of charges")
    print(f"{'ladder':<18}{'Oct 1-15':>11}{'Oct 16-31':>11}{'month':>11}{'win %':>7}{'worst day':>11}{'green days':>11}")
    for k in LADDERS:
        d = per_day[k].values()
        print(f"{k:<18}{rupees(res[k]['1-15']):>11}{rupees(res[k]['16-31']):>11}{rupees(res[k]['all']):>11}"
              f"{100 * res[k]['wins'] / n:>6.0f}%{rupees(min(d)):>11}{sum(x > 0 for x in d):>6}/{len(d)}")


if __name__ == "__main__":
    main()
