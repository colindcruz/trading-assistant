import re
import threading

from ta.exit_engine import ExitEngine
from ta.exit_logic import build_instrument_index, lpp_floor, round_to_tick
from helpers import Clock, FakeBroker, INSTRUMENT_ROWS, NIFTY, NIFTY2, SENSEX, STOCK, make_settings

INSTR = build_instrument_index(INSTRUMENT_ROWS)


def engine(broker, clock=None, **over):
    clock = clock or Clock()
    notes, orders = [], []
    eng = ExitEngine(broker, make_settings(**over), INSTR, lambda t, urgent=False: notes.append((t, urgent)),
                     orders.append, clock=clock, sleep=clock.sleep)
    return eng, notes, orders


def test_exit_all_cancels_resting_orders_then_sells_at_bid_minus_buffer_sliced_to_freeze():
    b = FakeBroker(bids={"NFO:NIFTY26N0525000CE": 100.0})
    b.add_position(NIFTY, 7020, 90, last_price=100)         # 108 lots: needs 2 slices (3510 + 3510)
    sl = b.add_resting_order(NIFTY, "SL", "SELL", 7020)
    eng, notes, orders = engine(b)
    r = eng.exit_all("loss limit")
    assert b.cancelled == [sl]
    assert [o["quantity"] for o in b.placed] == [3510, 3510]
    assert all(o["order_type"] == "LIMIT" and o["price"] == 95.0 for o in b.placed)
    tags = [o["tag"] for o in b.placed]
    assert len(set(tags)) == 2 and all(re.fullmatch(r"[A-Za-z0-9]{1,20}", t) for t in tags)
    assert r.flat and not r.failed and r.cancelled == 1
    assert [o["action"] for o in orders][:3] == ["cancel", "place", "place"]
    assert any(o["action"] == "result" and o["status"] == "COMPLETE" for o in orders)


def test_sensex_uses_its_own_freeze_and_lot():
    b = FakeBroker()
    b.add_position(SENSEX, 1500, 400)                       # 75 lots of 20 -> 1000 + 500
    eng, _, _ = engine(b)
    r = eng.exit_all("eod")
    assert [o["quantity"] for o in b.placed] == [1000, 500] and r.flat


def test_stock_options_and_shorts_are_never_touched():
    b = FakeBroker()
    b.add_position(STOCK, 500, 50)
    b.add_position(NIFTY2, -130, 80)
    eng, notes, _ = engine(b)
    r = eng.exit_all("loss limit")
    assert r.nothing_to_do and b.placed == []
    assert r.shorts == [NIFTY2["tradingsymbol"]]
    assert any("Short position" in t and urgent for t, urgent in notes)
    eng.exit_all("again")
    assert sum("Short position" in t for t, _ in notes) == 1   # reported once


def test_unfilled_orders_are_repriced_three_times_then_urgent():
    b = FakeBroker(fill_mode="never", bids={"NFO:NIFTY26N0525000CE": 100.0})
    b.add_position(NIFTY, 650, 90, last_price=100)
    clock = Clock()
    eng, notes, _ = engine(b, clock)
    r = eng.exit_all("loss limit")
    oid = b.placed[0]["order_id"]
    assert [m[0] for m in b.modified] == [oid, oid, oid]
    prices = [m[1] for m in b.modified]
    assert prices == sorted(prices, reverse=True) and prices[-1] < 95.0
    assert any("EXIT MANUALLY" in t and urgent for t, urgent in notes)
    assert not r.flat and r.still_open == ["NIFTY26N0525000CE x650"]
    assert 60 <= r.elapsed < 70


def test_price_never_below_lpp_band_or_tick():
    b = FakeBroker(fill_mode="never", bids={"NFO:NIFTY26N0525000CE": 10.0})
    b.add_position(NIFTY, 65, 10, last_price=10)
    eng, _, _ = engine(b, exit_buffer_pct=30.0)
    eng.exit_all("x")
    for _, price in b.modified:
        assert price >= 0.05
    b2 = FakeBroker(fill_mode="never", bids={"NFO:NIFTY26N0525000CE": 200.0})
    b2.add_position(NIFTY, 65, 200, last_price=200)
    eng2, _, _ = engine(b2, exit_buffer_pct=35.0)
    eng2.exit_all("x")
    assert all(p >= round_to_tick(lpp_floor(200.0)) for _, p in b2.modified)


def test_rejected_order_is_resent_once_and_fills():
    calls = {"n": 0}

    def mode(o):
        calls["n"] += 1
        return "reject" if calls["n"] == 1 else "immediate"

    b = FakeBroker(fill_mode=mode)
    b.add_position(NIFTY, 65, 100)
    eng, _, orders = engine(b)
    r = eng.exit_all("loss limit")
    assert len(b.placed) == 2 and r.flat
    assert any(o["action"] == "result" and o["status"] == "REJECTED" for o in orders)
    assert r.failed and "rejected" in r.failed[0]


def test_limit_failure_falls_back_to_market_with_protection():
    b = FakeBroker()
    b.add_position(NIFTY, 65, 100)
    b.raise_on_limit = RuntimeError("price out of range")
    eng, _, orders = engine(b)
    r = eng.exit_all("loss limit")
    assert b.placed[0]["order_type"] == "MARKET" and r.flat
    assert orders[0]["type"] == "MARKET" and "limit_error" in orders[0]


def test_both_placements_failing_is_reported_not_raised():
    b = FakeBroker()
    b.add_position(NIFTY, 65, 100)
    b.raise_on_limit = RuntimeError("no")
    b.raise_on_market = RuntimeError("no market either")
    eng, notes, _ = engine(b)
    r = eng.exit_all("loss limit")
    assert r.failed and "no market either" in r.failed[0] and not r.flat


def test_second_sweep_while_running_is_refused():
    b = FakeBroker()
    b.add_position(NIFTY, 65, 100)
    eng, _, _ = engine(b)
    assert eng._sweep_lock.acquire()
    try:
        r = eng.exit_all("second")
        assert r.already_running and b.placed == []
    finally:
        eng._sweep_lock.release()


def test_square_off_one_contract_with_quantity_cap():
    b = FakeBroker()
    b.add_position(NIFTY, 650, 100)
    b.add_position(SENSEX, 100, 400)
    eng, _, _ = engine(b)
    r = eng.square_off("NFO:NIFTY26N0525000CE:MIS", "lockout", qty=130)
    assert [(o["tradingsymbol"], o["quantity"]) for o in b.placed] == [("NIFTY26N0525000CE", 130)]
    assert r.flat   # the key we targeted is reduced; the sweep only watches its keys
    assert b.positions[0]["quantity"] == 520 and b.positions[1]["quantity"] == 100


def test_skip_below_price_is_explicit_and_reported():
    b = FakeBroker()
    b.add_position(NIFTY, 65, 2.0, last_price=2.0)
    eng, _, _ = engine(b, skip_below_price=5.0)
    r = eng.exit_all("eod")
    assert r.nothing_to_do and r.skipped and "below 5.0" in r.skipped[0] and b.placed == []


def test_own_resting_sell_caps_quantity():
    """A TA order already resting on the contract (e.g. from a sweep interrupted by a
    restart) must not be doubled: cancel-wait removes it, but if the cancel fails the
    remaining pending quantity still clamps the new order."""
    b = FakeBroker()
    b.add_position(NIFTY, 650, 100)
    oid = b.add_resting_order(NIFTY, "LIMIT", "SELL", 325, tag="TA09300101")
    original_cancel = b.cancel

    def failing_cancel(order_id, variety="regular"):
        raise RuntimeError("cancel failed")

    b.cancel = failing_cancel
    eng, _, _ = engine(b, cancel_wait_seconds=0.0)
    eng.exit_all("loss limit")
    assert b.placed[0]["quantity"] == 325
    b.cancel = original_cancel


def test_concurrent_calls_only_one_runs():
    import time as _time
    b = FakeBroker(fill_mode="never")
    b.add_position(NIFTY, 65, 100)
    clock = Clock()
    eng, _, _ = engine(b, clock)
    eng.sleep = lambda s: (_time.sleep(0.005), clock.sleep(s))   # real pause so the threads overlap
    results = []
    threads = [threading.Thread(target=lambda: results.append(eng.exit_all("t"))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if r.already_running) == 3
