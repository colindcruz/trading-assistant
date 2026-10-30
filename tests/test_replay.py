"""The replay must agree with an independent hand calculation within Rs 5k per day."""

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "replay"))

from replay_october import read_tradebook, replay  # noqa: E402

COLUMNS = ["symbol", "isin", "trade_date", "exchange", "segment", "series", "trade_type", "auction",
           "quantity", "price", "trade_id", "order_id", "order_execution_time"]


def write_tradebook(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for i, (day, t, sym, side, qty, price) in enumerate(rows, 1):
            w.writerow([sym, "", day, "NFO", "FO", "", side, "false", qty, price, f"T{i}", f"O{i}", f"{day}T{t}"])


def test_profit_giveback_day_is_cut_by_the_lock(tmp_path):
    """Hand calculation. Buy 6500 @ 100 (6.5L). Sell 6500 @ 140: booked +2.6L gross.
    Lock arms (>= 2L) with floor 1.334L; peak 2.6L >= trail start 2.5L -> floor 2.6L-68k = 1.92L.
    Buy 6500 @ 120, sell @ 85: a 2.275L loss takes the day to about +0.3L actual.
    The guardian locks when the sell at 85 is applied: net <= floor. So guardian net is the
    booked total at that fill (+2.6L - 2.275L = +0.325L)... except the exit fires on the
    first evaluation where net <= floor, which is this same fill (open positions are marked
    at last fill price, so the open loss only shows once a fill prints at 85)."""
    p = tmp_path / "tb.csv"
    write_tradebook(p, [
        ("2026-10-07", "09:20:00", "NIFTY26O0725000CE", "buy", 6500, 100),
        ("2026-10-07", "10:00:00", "NIFTY26O0725000CE", "sell", 6500, 140),
        ("2026-10-07", "11:00:00", "NIFTY26O0725000CE", "buy", 6500, 120),
        ("2026-10-07", "12:00:00", "NIFTY26O0725000CE", "sell", 6500, 85),
    ])
    fills = read_tradebook(p)
    assert len(fills) == 4
    (day,) = replay(fills, 20_00_000)
    assert day["trades"] == 2
    gross_actual = 6500 * 40 - 6500 * 35
    assert abs(day["actual_gross"] - gross_actual) < 1
    # charges: STT 0.15% on sells (6500*140 + 6500*85) + exchange/GST/stamp on all + 4 orders brokerage
    sells = 6500 * 140 + 6500 * 85
    buys = 6500 * 100 + 6500 * 120
    stt = sells * 0.0015
    exch = (sells + buys) * 0.0003553
    sebi = (sells + buys) / 1e7 * 10
    gst = (exch + sebi) * 0.18
    stamp = buys * 0.00003
    brokerage = 4 * 20 * 1.18
    expected_charges = stt + exch + sebi + gst + stamp + brokerage
    assert abs(day["actual_charges"] - expected_charges) < 5
    assert abs(day["actual_net"] - (gross_actual - expected_charges)) < 5000
    assert day["exit"] and day["exit"]["reason"] == "LOCK_EXIT"
    assert day["floor"] is not None
    # the guardian's day ends at the lock exit; it cannot be worse than the actual day here
    assert day["guardian_net"] >= day["actual_net"] - 5000


def test_loss_limit_day_is_capped_near_minus_2L(tmp_path):
    p = tmp_path / "tb.csv"
    rows = [("2026-10-09", "09:20:00", "NIFTY26O0924500PE", "buy", 6500, 200)]
    # sell in five slices at falling prices; the loss limit should fire part-way
    for i, px in enumerate([190, 175, 160, 140, 120], 1):
        rows.append(("2026-10-09", f"10:{i:02d}:00", "NIFTY26O0924500PE", "sell", 1300, px))
    rows.append(("2026-10-09", "11:00:00", "NIFTY26O0924500PE", "buy", 6500, 100))
    rows.append(("2026-10-09", "12:00:00", "NIFTY26O0924500PE", "sell", 6500, 60))
    write_tradebook(p, rows)
    (day,) = replay(read_tradebook(p), 20_00_000)
    assert day["exit"] and day["exit"]["reason"] == "LOSS_EXIT"
    assert day["actual_net"] < -500_000
    # the guardian stops at or just past -2L (the breach is only seen when a fill prints)
    assert -300_000 < day["guardian_net"] <= -150_000
    assert day["guardian_net"] > day["actual_net"]


def test_quiet_day_passes_through(tmp_path):
    p = tmp_path / "tb.csv"
    write_tradebook(p, [
        ("2026-10-13", "09:30:00", "SENSEX26O1682000CE", "buy", 200, 300),
        ("2026-10-13", "10:30:00", "SENSEX26O1682000CE", "sell", 200, 320),
    ])
    (day,) = replay(read_tradebook(p), 20_00_000)
    assert day["exit"] is None and day["trades"] == 1
    assert abs(day["guardian_net"] - day["actual_net"]) < 1
    assert 3000 < day["actual_net"] < 4000
