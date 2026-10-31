"""Per-trade stops through the Service: armed on the fill, moved and hit on ticks, the exit
only in live mode with STOP_EXITS=live, and a saved stop surviving a restart."""

from helpers import FakeBroker, NIFTY, NIFTY2, ist
from test_service import advance, build, order_update

NKEY = "NFO:NIFTY26N0525000CE:MIS"


def tick(svc, inst, price):
    svc.on_ticks([{"instrument_token": inst["instrument_token"], "last_price": price}])


def start(tmp_path, b=None, live=False, **settings):
    b = b or FakeBroker()
    svc, tg, _, holder, clock = build(tmp_path, b, now=ist(2026, 11, 2, 9, 5), **settings)
    svc.tick()
    if live:
        svc.handle_command("/live", holder["now"])
    holder["now"] = ist(2026, 11, 2, 10, 0)
    return svc, tg, b, holder, clock


def buy(svc, b, inst, qty, price, oid="buy1"):
    tick(svc, inst, price)
    svc.on_order_update(order_update(inst, qty, price, oid=oid))
    b.add_position(inst, qty, price)


def test_armed_moved_and_hit_in_log_mode_sells_nothing(tmp_path):
    svc, tg, b, holder, clock = start(tmp_path)
    buy(svc, b, NIFTY, 2600, 200.0)
    advance(svc, holder, clock)
    assert any("Stop armed: NIFTY26N0525000CE x2600 @ 200, stop 180 (v1)" in t for t in tg.sent)
    for p in (220.0, 232.0):
        tick(svc, NIFTY, p)
        advance(svc, holder, clock)
    assert svc.state.stops[NKEY]["ladders"]["v1"]["stop"] == 212.0
    assert any("Stop moved" in t and "-> 200" in t for t in tg.sent)
    tick(svc, NIFTY, 211.0)
    advance(svc, holder, clock)
    assert any("Stop hit" in t and "Not sold: stop exits are in LOG mode" in t for t in tg.sent)
    assert not [o for o in svc.state.orders if o["action"] == "place"]
    # you sell it yourself later: the trade's comparison row is logged
    svc.on_order_update(order_update(NIFTY, 2600, 214.0, side="SELL", oid="sell1"))
    advance(svc, holder, clock)
    row = svc.state.stop_log[-1]
    assert row["actual_exit"] == 214.0 and row["ladders"]["v1"]["hit_price"] == 211.0
    assert row["ladders"]["wide"]["hit_at"] is None and NKEY not in svc.state.stops


def test_live_with_stop_exits_live_sells_only_that_contract(tmp_path):
    b = FakeBroker(bids={"NFO:NIFTY26N0525000CE": 178.0})
    svc, tg, b, holder, clock = start(tmp_path, b, live=True, stop_ladder="v1", stop_exits="live")
    buy(svc, b, NIFTY, 2600, 200.0)
    buy(svc, b, NIFTY2, 130, 50.0, oid="buy2")
    advance(svc, holder, clock)
    tick(svc, NIFTY, 179.0)
    b.positions[0]["last_price"] = 179.0
    advance(svc, holder, clock)
    assert any("Stop hit" in t and "Selling." in t for t in tg.sent)
    placed = [o for o in b.placed]
    assert placed and {o["tradingsymbol"] for o in placed} == {NIFTY["tradingsymbol"]}
    assert sum(o["quantity"] for o in placed) == 2600


def test_dry_run_mode_does_not_sell_even_with_stop_exits_live(tmp_path):
    svc, tg, b, holder, clock = start(tmp_path, stop_exits="live")
    buy(svc, b, NIFTY, 2600, 200.0)
    advance(svc, holder, clock)
    tick(svc, NIFTY, 179.0)
    advance(svc, holder, clock)
    assert any("Not sold: the TA is in DRY-RUN" in t for t in tg.sent)
    assert not [o for o in svc.state.orders if o["action"] == "place"]


def test_add_alerts_and_ratchets(tmp_path):
    svc, tg, b, holder, clock = start(tmp_path)
    buy(svc, b, NIFTY, 1300, 200.0)
    advance(svc, holder, clock)
    tick(svc, NIFTY, 230.0)
    svc.on_order_update(order_update(NIFTY, 1300, 230.0, oid="add1"))
    advance(svc, holder, clock)
    # the add is applied before that second's price: new average 215 - 20 = 195; then the trail (230 - 20) lifts it
    assert any("Add detected" in t and "stop ratcheted to 195" in t for t in tg.sent)
    assert svc.state.stops[NKEY]["qty"] == 2600 and svc.state.stops[NKEY]["ladders"]["v1"]["stop"] == 210.0


def test_saved_stop_survives_a_restart(tmp_path):
    b = FakeBroker()
    svc, tg, b, holder, clock = start(tmp_path, b)
    buy(svc, b, NIFTY, 2600, 200.0)
    b.trades_rows.append({"trade_id": "T1", "order_id": "buy1", "exchange": "NFO", "tradingsymbol": NIFTY["tradingsymbol"],
                          "product": "MIS", "transaction_type": "BUY", "quantity": 2600, "average_price": 200.0,
                          "fill_timestamp": "2026-11-02 10:00:00"})
    advance(svc, holder, clock)
    tick(svc, NIFTY, 240.0)
    advance(svc, holder, clock)
    assert svc.state.stops[NKEY]["ladders"]["v1"]["stop"] == 220.0
    svc.dirty = True
    clock.t += 10
    svc._persist()
    svc2, tg2, _, holder2, clock2 = build(tmp_path, b, now=ist(2026, 11, 2, 10, 5))
    svc2.tick()
    assert svc2.state.stops[NKEY]["ladders"]["v1"]["stop"] == 220.0       # not re-armed at 180
    assert not any("Stop armed" in t for t in tg2.sent)


def test_position_without_a_stop_raises_the_alarm(tmp_path):
    svc, tg, b, holder, clock = start(tmp_path)
    buy(svc, b, NIFTY, 2600, 200.0)
    advance(svc, holder, clock)
    svc.state.stops.clear()                    # simulate a lost stop
    advance(svc, holder, clock, ticks=3)
    assert sum("UNPROTECTED: NIFTY26N0525000CE" in t for t in tg.sent) == 1


def test_status_lists_stops_and_summary_compares(tmp_path):
    svc, tg, b, holder, clock = start(tmp_path)
    buy(svc, b, NIFTY, 2600, 200.0)
    advance(svc, holder, clock)
    status = svc.handle_command("/status", holder["now"])
    assert "Stops (v1, exits LOG):" in status and "stop 180 (initial); wide 180" in status
    svc.on_order_update(order_update(NIFTY, 2600, 210.0, side="SELL", oid="sell1"))
    advance(svc, holder, clock)
    holder["now"] = ist(2026, 11, 2, 15, 31)
    svc.tick()
    assert any("Stops on 1 trades (gross): v1 ₹26,000, wide ₹26,000; actual ₹26,000" in t for t in tg.sent)
