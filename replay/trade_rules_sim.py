"""
Per-trade simulation: what if every FIRST entry had been managed by the stop /
take-profit / trailing ladder, with no averaging or pyramiding?

    python replay/trade_rules_sim.py tradebook.xlsx archive_dir 2026-10-12

A trade = the first BUY order of a contract while flat (all of that order's fills,
averaged). Later adds are ignored. The trade is then driven only by 1-minute candles:
  stop        entry - 20 (NIFTY) / entry - 50 (SENSEX)
  T1          +15: sell half at the target, stop -> entry + 1
  +20         stop trails highest - 8 (continuous, never relaxes)
  +30         runner: stop trails highest - 6
  15:06       whatever is left is sold at that minute's open
SENSEX rungs are the NIFTY ones x2.5 (same ratio as the 50/20 stops).
A new actual entry in the same contract counts as a new trade only once the actual
position was flat; if the simulated trade is still open at that time, the new entry is
skipped and reported. Candle order convention: the stop is checked against the low
before the high can lift it.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from replay_october import read_tradebook  # noqa: E402
from ta.clock import IST, rupees  # noqa: E402
from ta.config import ChargeRates  # noqa: E402
from ta.ledger import Fill, charges_for_fill  # noqa: E402

FIXED_QTY: dict[str, int] = {}     # exchange -> quantity; empty = use the first order's own quantity
RATES = ChargeRates()

RULE_SETS = {
    # the long-option engine's ladder: T1 sells half at +15, trail 8 from +20, runner trail 6 from +30
    "engine": {
        "NFO": dict(stop=20, t1=15, t1_pct=0.5, post_t1=1, pl_trigger=20, pl_trail=8, runner_trigger=30, runner_trail=6),
        "BFO": dict(stop=50, t1=37.5, t1_pct=0.5, post_t1=2.5, pl_trigger=50, pl_trail=20, runner_trigger=75, runner_trail=15),
    },
    # Trading rules v1 (spec 2026-10-31): breakeven at +20/+50, from +30/+75 trail 20/50 below the high, no scale-outs
    "v1": {
        "NFO": dict(stop=20, t1=20, t1_pct=0.0, post_t1=0, pl_trigger=30, pl_trail=20, runner_trigger=1e9, runner_trail=20),
        "BFO": dict(stop=50, t1=50, t1_pct=0.0, post_t1=0, pl_trigger=75, pl_trail=50, runner_trigger=1e9, runner_trail=50),
    },
}
RULES = RULE_SETS["engine"]
EXIT_ALL = (15, 6)


def load_candles(archive: Path, day: str) -> dict[str, list[dict]]:
    out = {}
    for u in ("NIFTY", "SENSEX"):
        f = archive / u / f"{day}.json"
        if not f.exists():
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        for exp in d["expiries"].values():
            for sym, info in exp["options"].items():
                cs = []
                for c in info["candles"]:
                    t = datetime.fromisoformat(c["t"])
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=IST)
                    cs.append({"t": t, "o": float(c["o"]), "h": float(c["h"]), "l": float(c["l"]), "c": float(c["c"])})
                out[sym.upper()] = sorted(cs, key=lambda x: x["t"])
    return out


def actual_trades(fills):
    """Flat-to-flat cycles per contract from the real tradebook."""
    pos = defaultdict(int)
    avg = defaultdict(float)
    booked = defaultdict(float)
    open_trade = {}
    trades = []
    for f in fills:
        s = f.tradingsymbol
        if f.side == "BUY":
            if pos[s] == 0:
                open_trade[s] = {"symbol": s, "exchange": f.exchange, "entry_ts": f.ts, "entry_order": f.order_id,
                                 "first_qty": 0, "first_value": 0.0, "bought": 0}
                booked[s] = 0.0
            t = open_trade[s]
            if f.order_id == t["entry_order"]:
                t["first_qty"] += f.quantity
                t["first_value"] += f.value
            t["bought"] += f.quantity
            avg[s] = (avg[s] * pos[s] + f.price * f.quantity) / (pos[s] + f.quantity)
            pos[s] += f.quantity
        else:
            if pos[s] <= 0:
                continue
            q = min(f.quantity, pos[s])
            booked[s] += (f.price - avg[s]) * q
            pos[s] -= q
            t = open_trade.get(s)
            if t is not None and pos[s] == 0:
                t["exit_ts"] = f.ts
                t["actual_pnl"] = booked[s]
                trades.append(t)
                del open_trade[s]
    for s, t in open_trade.items():
        t["exit_ts"] = None
        t["actual_pnl"] = booked[s]
        trades.append(t)
    return sorted(trades, key=lambda t: t["entry_ts"])


def trade_charges(trade, entry, qty, exits):
    """Entry BUY plus one SELL per exit leg, charged like the live ledger (brokerage per order)."""
    ts = trade["entry_ts"]
    ex, sym = trade["exchange"], trade["symbol"]
    total = charges_for_fill(Fill("e", "e", ex, sym, "MIS", "BUY", qty, entry, ts), RATES)
    n_orders = 1
    for px, q in exits:
        if q > 0:
            total += charges_for_fill(Fill("x", "x", ex, sym, "MIS", "SELL", q, px, ts), RATES)
            n_orders += 1
    return total + n_orders * RATES.brokerage_per_order * (1 + RATES.gst_pct / 100)


def simulate(trade, candles):
    r = RULES[trade["exchange"]]
    entry = trade["first_value"] / trade["first_qty"]
    qty = FIXED_QTY.get(trade["exchange"], trade["first_qty"])
    remaining = qty
    stop = entry - r["stop"]
    highest = entry
    t1_done = False
    state = "INITIAL"
    pnl = 0.0
    log = []
    exit_all_ts = trade["entry_ts"].replace(hour=EXIT_ALL[0], minute=EXIT_ALL[1], second=0, microsecond=0)
    for c in candles:
        if c["t"] <= trade["entry_ts"]:
            continue
        if c["t"] >= exit_all_ts:
            px = c["o"]
            pnl += (px - entry) * remaining
            log.append((c["t"], "EOD", px, remaining))
            remaining = 0
            break
        # 1. stop against the low (a gap through the stop fills at the open)
        if c["l"] <= stop:
            px = c["o"] if c["o"] < stop else stop
            pnl += (px - entry) * remaining
            log.append((c["t"], "STOP_" + state, px, remaining))
            remaining = 0
            break
        # 2. targets and ratchets off the high
        highest = max(highest, c["h"])
        gain = highest - entry
        if not t1_done and gain >= r["t1"]:
            t1_done = True
            px = entry + r["t1"]
            sell = int(remaining * r["t1_pct"])
            pnl += (px - entry) * sell
            remaining -= sell
            stop = max(stop, entry + r["post_t1"])
            state = "T1"
            log.append((c["t"], "T1_HALF", px, sell))
        if gain >= r["runner_trigger"]:
            state = "RUNNER"
            stop = max(stop, highest - r["runner_trail"])
        elif gain >= r["pl_trigger"]:
            state = "PL20"
            stop = max(stop, highest - r["pl_trail"])
    charges = trade_charges(trade, entry, qty, [(l[2], l[3]) for l in log])
    return {"entry": entry, "qty": qty, "pnl": pnl, "charges": charges, "net": pnl - charges,
            "remaining": remaining, "log": log, "highest": highest}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    tb, archive, day = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    for arg in sys.argv[4:]:
        if arg.startswith("--rules="):
            RULES.clear()
            RULES.update(RULE_SETS[arg.split("=", 1)[1]])
        if arg.startswith("--fixed-qty="):           # --fixed-qty=NIFTY:3250,SENSEX:1500
            for part in arg.split("=", 1)[1].split(","):
                name, q = part.split(":")
                FIXED_QTY["NFO" if name.upper() == "NIFTY" else "BFO"] = int(q)
    fills = [f for f in read_tradebook(tb) if f.ts.date().isoformat() == day]
    candles = load_candles(archive, day)
    trades = actual_trades(fills)
    print(f"{day}: {len(fills)} fills, {len(trades)} flat-to-flat trades, actual day P&L (gross) "
          f"{rupees(sum(t['actual_pnl'] for t in trades))}")
    print(f"{'entry':<9}{'contract':<20}{'qty':>6}{'@':>8} {'exit':<6}{'reason':<13}{'high':>8}{'rule P&L':>11}{'actual':>11}  notes")
    sim_total = act_total = 0.0
    sim_charges = 0.0
    open_until = {}
    for t in trades:
        s = t["symbol"]
        if s not in candles:
            print(f"{t['entry_ts']:%H:%M:%S} {s:<20} no minute data; skipped (actual {rupees(t['actual_pnl'])})")
            act_total += t["actual_pnl"]
            continue
        overlap = ""
        if s in open_until and t["entry_ts"] < open_until[s]:
            overlap = f" (overlaps the earlier rule trade until {open_until[s]:%H:%M})"
        res = simulate(t, candles[s])
        last = res["log"][-1] if res["log"] else None
        if last:
            open_until[s] = last[0]
        reason = last[1] if last else "OPEN"
        exit_t = f"{last[0]:%H:%M}" if last else "-"
        notes = " ".join(f"T1@{l[2]:.1f}x{l[3]}" for l in res["log"] if l[1] == "T1_HALF")
        extra = f" (actual bought {t['bought']} in total)" if t["bought"] > t["first_qty"] else ""
        print(f"{t['entry_ts']:%H:%M:%S} {s:<20}{res['qty']:>6}{res['entry']:>8.1f} {exit_t:<6}{reason:<13}{res['highest']:>8.1f}"
              f"{rupees(res['pnl']):>11}{rupees(t['actual_pnl']):>11}  {notes}{extra}{overlap}")
        sim_total += res["pnl"]
        sim_charges += res["charges"]
        act_total += t["actual_pnl"]
    print(f"\nRule-managed total (gross, first entries only): {rupees(sim_total)}    actual (gross): {rupees(act_total)}")
    print(f"Rule-managed charges {rupees(sim_charges)}; net {rupees(sim_total - sim_charges)}"
          + (f"; fixed qty {FIXED_QTY}" if FIXED_QTY else ""))


if __name__ == "__main__":
    main()
