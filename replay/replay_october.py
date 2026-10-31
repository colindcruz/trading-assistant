"""
Replay a Zerodha tradebook through the ledger and rules (spec acceptance test 1).

    python replay/replay_october.py tradebook.xlsx [--capital 2000000] [--archive DIR] [--json out.json]

Input: the Console tradebook export (.xlsx or .csv): one row per fill with symbol,
exchange, trade_type, quantity, price, trade_id, order_id, order_execution_time.

Marks between fills:
  * With --archive DIR (the droplet's option_data_archive: DIR/NIFTY/<date>.json and
    DIR/SENSEX/<date>.json, 1-minute candles per contract), open positions are marked
    every minute at that minute's close. This is what the live TA would see.
  * Without it, open positions are marked only at the last fill price of the contract
    (the spec's original method). Losses then show up late and all at once, so exits
    overshoot. Direction only.

Guardian timeline per day: fills and minute ticks are processed in time order. At every
step the rules run on net P&L (ledger + charges). An exit-all closes the guardian's open
positions virtually at the current mark. After a profit-lock exit the day continues:
fills during the cool-down are squared off at once (as live), later BUY fills open new
trades, and SELL fills of contracts the guardian no longer holds are ignored. A loss
limit, the 4th trade closing, or 15:06 ends the guardian's day.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ta.clock import IST, rupees  # noqa: E402
from ta.config import ChargeRates, RulePercents, Thresholds  # noqa: E402
from ta.ledger import Fill, Ledger  # noqa: E402
from ta.rules import DayState, evaluate, on_ledger_event  # noqa: E402

EXIT_ALL = (15, 6)
NO_NEW = (15, 10)


# ---------------------------------------------------------------- tradebook

def _rows_from_xlsx(path: Path) -> list[dict]:
    """Zerodha's Console .xlsx export: a header block, then a 'Symbol, ISIN, Trade Date, ...'
    row, then one row per fill. Column names are normalised to the CSV export's."""
    import openpyxl  # optional dependency, only for .xlsx input
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.worksheets[0]
    header = None       # column index -> normalised name
    out = []
    for row in ws.iter_rows(values_only=True):
        cells = ["" if c is None else c for c in row]
        if header is None:
            if any(str(c).strip().lower() == "symbol" for c in cells):
                header = {i: str(c).strip().lower().replace(" ", "_") for i, c in enumerate(cells) if str(c).strip()}
            continue
        rec = {name: (cells[i] if i < len(cells) else "") for i, name in header.items()}
        if not str(rec.get("symbol", "")).strip():
            continue
        out.append(rec)
    return out


def read_tradebook(path: Path) -> list[Fill]:
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        rows = _rows_from_xlsx(path)
    else:
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    return parse_rows(rows)


def parse_rows(rows: list[dict]) -> list[Fill]:
    fills = []
    for row in rows:
        row = {k: ("" if v is None else v) for k, v in row.items()}
        sym = str(row.get("symbol") or row.get("tradingsymbol") or "").strip().upper()
        if not sym.endswith(("CE", "PE")) or not (sym.startswith("NIFTY") or sym.startswith("SENSEX")):
            continue
        if sym.startswith("NIFTYNXT"):
            continue
        exchange = str(row.get("exchange") or ("BFO" if sym.startswith("SENSEX") else "NFO")).upper()
        exchange = {"NSE": "NFO", "BSE": "BFO"}.get(exchange, exchange)
        ts_raw = row.get("order_execution_time") or row.get("trade_date")
        ts = ts_raw if isinstance(ts_raw, datetime) else datetime.fromisoformat(str(ts_raw).replace("T", " "))
        ts = ts.replace(tzinfo=IST)
        fills.append(Fill(
            fill_id=str(row.get("trade_id") or f"{row.get('order_id')}:{ts_raw}"),
            order_id=str(row.get("order_id") or ""),
            exchange=exchange, tradingsymbol=sym,
            product=str(row.get("product") or "MIS").upper(),
            side=str(row.get("trade_type") or "").strip().upper(),
            quantity=int(float(row["quantity"])),
            price=float(row["price"]),
            ts=ts,
        ))
    return sorted(fills, key=lambda x: (x.ts, x.fill_id))


# ---------------------------------------------------------------- minute marks

class DayMarks:
    """1-minute closes per tradingsymbol for one day, from the archive files."""

    def __init__(self, files: list[Path]):
        self.series: dict[str, list[tuple[datetime, float]]] = {}
        for f in files:
            if not f.exists():
                continue
            d = json.loads(f.read_text(encoding="utf-8"))
            for exp in (d.get("expiries") or {}).values():
                for sym, info in (exp.get("options") or {}).items():
                    pts = []
                    for c in info.get("candles") or []:
                        t = datetime.fromisoformat(c["t"])
                        if t.tzinfo is None:
                            t = t.replace(tzinfo=IST)
                        pts.append((t + timedelta(minutes=1), float(c["c"])))   # a candle's close is known at its end
                    if pts:
                        self.series[sym.upper()] = sorted(pts)

    def has(self, sym: str) -> bool:
        return sym in self.series

    def minutes(self) -> list[datetime]:
        out = set()
        for pts in self.series.values():
            for t, _ in pts:
                out.add(t)
        return sorted(out)

    def price_at(self, sym: str, ts: datetime) -> float | None:
        pts = self.series.get(sym)
        if not pts:
            return None
        # last close at or before ts
        lo, hi = 0, len(pts)
        while lo < hi:
            mid = (lo + hi) // 2
            if pts[mid][0] <= ts:
                lo = mid + 1
            else:
                hi = mid
        return pts[lo - 1][1] if lo > 0 else None


def load_marks(archive: Path | None, day: str) -> DayMarks | None:
    if archive is None:
        return None
    return DayMarks([archive / "NIFTY" / f"{day}.json", archive / "SENSEX" / f"{day}.json"])


# ---------------------------------------------------------------- the replay

def replay_day(day: str, fills: list[Fill], capital: float, rates: ChargeRates, percents: RulePercents,
               marks: DayMarks | None = None, target: float = 0.0, lock_ends_day: bool = False) -> dict:
    """What-ifs: `target` > 0 is a fixed profit target (net reaches it: exit all, day over);
    `lock_ends_day` makes a profit-lock exit end the day instead of starting a cool-down."""
    th = Thresholds.from_capital(capital, percents)
    guardian = Ledger(rates)
    actual = Ledger(rates)
    state = DayState(date=day, thresholds=th)
    last_price: dict[str, float] = {}      # "EX:SYM" -> mark (fill price, or minute close when archived)
    out = {"date": day, "fills": len(fills), "exits": [], "events": [], "uncovered": []}
    ended = False
    uncovered: set[str] = set()

    def mark_open_positions(ts: datetime) -> None:
        if marks is None:
            return
        for c in guardian.open_contracts():
            px = marks.price_at(c.tradingsymbol, ts)
            if px is not None:
                last_price[c.instrument_key] = px
            else:
                uncovered.add(c.tradingsymbol)

    def virtual_close(ts: datetime) -> None:
        for c in guardian.open_contracts():
            px = last_price.get(c.instrument_key, c.avg_cost)
            guardian.apply(Fill(f"virt:{c.key}:{ts:%H%M%S}", "virt", c.exchange, c.tradingsymbol, c.product,
                                "SELL", c.qty, px, ts))

    def check_rules(ts: datetime) -> None:
        nonlocal ended
        if ended:
            return
        net = guardian.net_pnl(last_price)
        if target > 0 and net >= target:
            out["exits"].append({"time": ts.strftime("%H:%M:%S"), "reason": "PROFIT_TARGET", "net": round(net)})
            virtual_close(ts)
            out["exits"][-1]["net_after_close"] = round(guardian.net_pnl(last_price))
            ended = True
            return
        for ev in evaluate(state, net, ts, bool(guardian.open_contracts()), EXIT_ALL):
            out["events"].append(f"{ts:%H:%M:%S} {ev.kind} net={net:.0f}")
            if ev.exit_all:
                out["exits"].append({"time": ts.strftime("%H:%M:%S"), "reason": ev.kind, "net": round(net)})
                virtual_close(ts)
                out["exits"][-1]["net_after_close"] = round(guardian.net_pnl(last_price))
            if ev.kind in ("LOSS_EXIT", "EOD_EXIT") or (lock_ends_day and ev.kind == "LOCK_EXIT"):
                ended = True
            if ev.exit_all or ended:
                break
        if state.lockout and not ended:
            ended = True

    def on_fill(fill: Fill) -> None:
        actual.apply(fill)
        last_price[f"{fill.exchange}:{fill.tradingsymbol}"] = fill.price
        if marks is not None and not marks.has(fill.tradingsymbol):
            uncovered.add(fill.tradingsymbol)
        if ended:
            return
        mark_open_positions(fill.ts)
        check_rules(fill.ts)
        if ended:
            return
        c = guardian.contracts.get(fill.key)
        if fill.side == "SELL" and (c is None or c.qty <= 0):
            return   # the guardian already closed this; the trader's later sell is not ours
        if fill.side == "BUY":
            # A trader who knows the rules does not buy while blocked, and does not open a
            # 5th trade. Live, such a fill is squared off on sight; here it is simply dropped,
            # because applying it and reversing it would charge phantom round trips.
            if state.blocked_reason(fill.ts, NO_NEW) is not None:
                out.setdefault("dropped_buys", 0)
                out["dropped_buys"] += 1
                return
            opens_new = c is None or c.qty == 0
            if opens_new and th.max_trades > 0 and state.trades_used(guardian.trades_opened) >= th.max_trades:
                out.setdefault("dropped_buys", 0)
                out["dropped_buys"] += 1
                return
        if fill.side == "SELL" and c is not None and fill.quantity > c.qty:
            fill = Fill(fill.fill_id, fill.order_id, fill.exchange, fill.tradingsymbol, fill.product, "SELL",
                        c.qty, fill.price, fill.ts)
        for lev in guardian.apply(fill):
            for ev in on_ledger_event(state, lev, fill.ts, guardian.trades_opened, NO_NEW):
                out["events"].append(f"{fill.ts:%H:%M:%S} {ev.kind}")
                if ev.kind == "TRADE_CAP_LOCKOUT":
                    out["exits"].append({"time": fill.ts.strftime("%H:%M:%S"), "reason": "TRADE_CAP",
                                         "net": round(guardian.net_pnl(last_price))})
                if ev.square_off:
                    q = ev.square_off_qty or guardian.contracts[ev.square_off].qty
                    guardian.apply(Fill(f"{fill.fill_id}:sq", fill.order_id + "sq", fill.exchange, fill.tradingsymbol,
                                        fill.product, "SELL", q, fill.price, fill.ts))
        check_rules(fill.ts)

    # timeline: fills and minute ticks in time order (fills first on a tie)
    timeline: list[tuple[datetime, int, object]] = [(f.ts, 0, f) for f in fills]
    if marks is not None:
        day_start = fills[0].ts.replace(hour=9, minute=15, second=0, microsecond=0) if fills else None
        for t in marks.minutes():
            if day_start is None or t >= day_start:
                timeline.append((t, 1, None))
    elif fills:
        timeline.append((fills[-1].ts.replace(hour=15, minute=6, second=0, microsecond=0), 1, None))
    timeline.sort(key=lambda x: (x[0], x[1]))
    for ts, kind, obj in timeline:
        if kind == 0:
            on_fill(obj)
        else:
            if ended:
                continue
            mark_open_positions(ts)
            check_rules(ts)
            if not ended and (ts.hour, ts.minute) >= (15, 30) and guardian.open_contracts():
                virtual_close(ts)
                ended = True

    out["guardian_net"] = round(guardian.net_pnl(last_price))
    out["actual_net"] = round(actual.net_pnl(last_price))
    out["actual_gross"] = round(actual.booked + actual.open_pnl(last_price))
    out["actual_charges"] = round(actual.total_charges)
    out["peak"] = round(state.peak)
    out["trough"] = round(state.trough)
    out["trades"] = actual.trades_opened
    out["guardian_trades"] = guardian.trades_opened
    out["floor"] = round(state.floor) if state.floor is not None else None
    out["reentries"] = state.reentries
    out["exit"] = out["exits"][0] if out["exits"] else None
    out["uncovered"] = sorted(uncovered)
    return out


def scale_fills(fills: list[Fill], factor: float) -> list[Fill]:
    """What-if: every fill at `factor` times its quantity, NOT rounded to lots, so buys and
    sells still net to exactly zero (rounding each partial fill invents phantom positions).
    Use a factor that is exact in binary (0.5, 0.25) so float quantities stay exact.
    Approximates Phase 2 sizing without changing the trader's decisions."""
    return [Fill(f.fill_id, f.order_id, f.exchange, f.tradingsymbol, f.product, f.side, f.quantity * factor, f.price, f.ts)
            for f in fills]


def replay(fills: list[Fill], capital: float, rates=ChargeRates(), percents=RulePercents(),
           archive: Path | None = None, target: float = 0.0, lock_ends_day: bool = False) -> list[dict]:
    by_day: dict[str, list[Fill]] = defaultdict(list)
    for f in fills:
        by_day[f.ts.date().isoformat()].append(f)
    return [replay_day(day, by_day[day], capital, rates, percents, load_marks(archive, day), target, lock_ends_day)
            for day in sorted(by_day)]


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")   # Windows consoles default to cp1252, which has no rupee sign
    ap = argparse.ArgumentParser()
    ap.add_argument("tradebook")
    ap.add_argument("--capital", type=float, default=20_00_000)
    ap.add_argument("--archive", default="", help="option_data_archive dir with NIFTY/ and SENSEX/ 1-minute files")
    ap.add_argument("--json", default="")
    ap.add_argument("--no-loss-limit", action="store_true", help="what-if: profit lock, trade cap and time rules only")
    ap.add_argument("--qty-scale", type=float, default=1.0, help="what-if: scale every fill's quantity (0.5, 0.25)")
    ap.add_argument("--max-trades", type=int, default=0, help="what-if: override the trade cap (0 = spec default 4)")
    ap.add_argument("--loss-limit-pct", type=float, default=0, help="what-if: loss limit as % of capital (0 = spec default 10)")
    args = ap.parse_args()
    fills = read_tradebook(Path(args.tradebook))
    if args.qty_scale != 1.0:
        fills = scale_fills(fills, args.qty_scale)
    percents = RulePercents()
    from dataclasses import replace as _replace
    if args.no_loss_limit:
        percents = _replace(percents, loss_limit_pct=1e6, loss_warn1_pct=1e6, loss_warn2_pct=1e6)
    if args.max_trades:
        percents = _replace(percents, max_trades=args.max_trades, reentry_floor_step=50_000.0)
    if args.loss_limit_pct:
        percents = _replace(percents, loss_limit_pct=args.loss_limit_pct, loss_warn1_pct=args.loss_limit_pct / 2,
                            loss_warn2_pct=args.loss_limit_pct * 0.75, reentry_floor_step=50_000.0)
    days = replay(fills, args.capital, percents=percents, archive=Path(args.archive) if args.archive else None)
    print(f"{'date':<12}{'fills':>6}{'trades':>7}{'actual':>12}{'guardian':>12}  exits")
    tot_a = tot_g = 0
    for d in days:
        tot_a += d["actual_net"]
        tot_g += d["guardian_net"]
        ex = "; ".join(f"{e['time']} {e['reason'].replace('_EXIT', '')}" for e in d["exits"]) or "-"
        ex += f"  [{d['guardian_trades']}/{d['trades']} trades]"
        unc = f"  (no minute data: {', '.join(d['uncovered'][:3])}{'...' if len(d['uncovered']) > 3 else ''})" if d["uncovered"] else ""
        print(f"{d['date']:<12}{d['fills']:>6}{d['trades']:>7}{rupees(d['actual_net']):>12}{rupees(d['guardian_net']):>12}  {ex}{unc}")
    print(f"{'TOTAL':<12}{'':>6}{'':>7}{rupees(tot_a):>12}{rupees(tot_g):>12}")
    if args.json:
        Path(args.json).write_text(json.dumps(days, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
