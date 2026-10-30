from datetime import timedelta

from ta.ledger import Fill, LedgerEvent
from ta.rules import DayState, evaluate, on_ledger_event
from helpers import ist, thresholds

EXIT_AT = (15, 6)
NO_NEW = (15, 10)


def state(capital=20_00_000):
    return DayState(date="2026-11-02", thresholds=thresholds(capital))


def run(st, net, now, has_open=True):
    return [e.kind for e in evaluate(st, net, now, has_open, EXIT_AT)]


def test_thresholds_at_20L():
    th = thresholds()
    assert (th.loss_limit, th.loss_warn1, th.loss_warn2) == (200_000, 100_000, 150_000)
    assert (th.lock_arm, th.lock_floor, th.trail_start, th.trail_distance) == (200_000, 133_400, 250_000, 68_000)
    assert th.max_trades == 4 and th.reentry_floor_step == 50_000


def test_loss_warnings_once_then_limit_locks_day():
    st = state()
    t = ist(2026, 11, 2, 10, 0)
    assert run(st, -100_000, t) == ["WARN1"]
    assert run(st, -120_000, t) == []
    assert run(st, -150_000, t) == ["WARN2"]
    ev = evaluate(st, -200_000, t, True, EXIT_AT)
    assert [e.kind for e in ev] == ["LOSS_EXIT"] and ev[0].exit_all and ev[0].urgent
    assert st.lockout and st.lockout_reason == "loss limit"
    assert run(st, -250_000, t) == []   # one shot


def test_loss_limit_with_flat_book_locks_without_exit():
    st = state()
    ev = evaluate(st, -200_000, ist(2026, 11, 2, 10, 0), False, EXIT_AT)
    assert [e.kind for e in ev] == ["WARN1", "WARN2", "LOSS_EXIT"]   # a straight drop fires all three at once
    assert not ev[-1].exit_all and st.lockout


def test_lock_arms_and_floor_ratchets_up_only():
    st = state()
    t = ist(2026, 11, 2, 11, 0)
    assert run(st, 150_000, t) == []
    assert run(st, 200_000, t) == ["LOCK_ARMED"]
    assert st.floor == 133_400
    assert run(st, 240_000, t) == []           # below trail start: floor unchanged
    assert st.floor == 133_400
    assert run(st, 260_000, t) == ["FLOOR_RAISED"]
    assert st.floor == 260_000 - 68_000
    assert run(st, 210_000, t) == []           # dip: floor stays
    assert st.floor == 192_000
    assert run(st, 270_000, t) == []           # +10k: raised silently (alert step is 25k)
    assert st.floor == 202_000
    assert run(st, 300_000, t) == ["FLOOR_RAISED"]
    assert st.floor == 232_000


def test_lock_exit_starts_cooldown_and_drops_floor_one_step():
    st = state()
    t = ist(2026, 11, 2, 11, 0)
    run(st, 200_000, t)
    run(st, 260_000, t)                        # floor 192k, peak 260k
    ev = evaluate(st, 192_000, t, True, EXIT_AT)
    assert [e.kind for e in ev] == ["LOCK_EXIT"] and ev[0].exit_all
    assert st.floor == 142_000 and st.reentries == 1 and not st.lockout
    assert st.in_cooldown(t) and st.cooldown_seconds_left(t) == 180
    # during cool-down the floor does not fire again, even with positions open
    assert run(st, 140_000, t + timedelta(seconds=30)) == []
    # ratchet is frozen until the peak is beaten
    assert run(st, 255_000, t + timedelta(seconds=40)) == []
    assert st.floor == 142_000
    # cool-down over
    assert run(st, 150_000, t + timedelta(minutes=3)) == ["COOLDOWN_OVER"]
    assert run(st, 150_000, t + timedelta(minutes=4)) == []
    # new peak above the old one: trail resumes
    assert run(st, 300_000, t + timedelta(minutes=5)) == ["FLOOR_RAISED"]
    assert st.floor == 232_000
    # second lock exit drops another step
    ev = evaluate(st, 232_000, t + timedelta(minutes=6), True, EXIT_AT)
    assert ev[0].kind == "LOCK_EXIT" and st.floor == 182_000 and st.reentries == 2


def test_floor_hit_with_flat_book_does_nothing():
    st = state()
    t = ist(2026, 11, 2, 11, 0)
    run(st, 200_000, t)
    assert run(st, 133_000, t, has_open=False) == []
    assert st.reentries == 0


def test_loss_limit_applies_during_cooldown():
    st = state()
    t = ist(2026, 11, 2, 11, 0)
    run(st, 200_000, t)
    evaluate(st, 133_000, t, True, EXIT_AT)    # lock exit, cooldown
    ev = evaluate(st, -200_000, t + timedelta(seconds=10), True, EXIT_AT)
    assert [e.kind for e in ev] == ["WARN1", "WARN2", "LOSS_EXIT"]


def test_eod_exit_at_1506_locks_day():
    st = state()
    ev = evaluate(st, 10_000, ist(2026, 11, 2, 15, 6), True, EXIT_AT)
    assert [e.kind for e in ev] == ["EOD_EXIT"] and ev[0].exit_all
    assert st.lockout and st.lockout_reason == "end of day"
    assert run(st, 10_000, ist(2026, 11, 2, 15, 7)) == []


def test_no_triggers_while_exiting():
    st = state()
    st.exiting = True
    assert run(st, -500_000, ist(2026, 11, 2, 10, 0)) == []


def fill(sym="NIFTY26N0525000CE", qty=65, price=100.0, side="BUY"):
    return Fill("x", "o", "NFO", sym, "MIS", side, qty, price, ist(2026, 11, 2, 10, 0))


def test_trade_counter_fifth_trade_squared_off_and_fourth_close_locks():
    st = state()
    t = ist(2026, 11, 2, 10, 0)
    kinds = [e.kind for e in on_ledger_event(st, LedgerEvent("TRADE_OPENED", "k", 4, fill()), t, 4, NO_NEW)]
    assert kinds == ["TRADE_OPENED"]
    ev = on_ledger_event(st, LedgerEvent("TRADE_OPENED", "k5", 5, fill()), t, 5, NO_NEW)
    assert ev[0].kind == "TRADE_CAP_SQUAREOFF" and ev[0].square_off == "k5" and ev[0].square_off_qty is None
    ev = on_ledger_event(st, LedgerEvent("TRADE_CLOSED", "k", 4, fill(side="SELL")), t, 5, NO_NEW)
    assert [e.kind for e in ev] == ["TRADE_CLOSED", "TRADE_CAP_LOCKOUT"]
    assert st.lockout and st.lockout_reason == "trade cap"


def test_squared_off_positions_do_not_count_toward_the_cap():
    """Three partial fills during a cool-down open three ledger trades that are all
    squared off on sight. Afterwards the trader still has all four trades available,
    and the fourth real trade's close is what locks the day."""
    st = state()
    t = ist(2026, 11, 2, 10, 0)
    st.cooldown_until = t + timedelta(minutes=3)
    for i in range(1, 4):
        ev = on_ledger_event(st, LedgerEvent("TRADE_OPENED", "k", i, fill()), t, i, NO_NEW)
        assert ev[0].kind == "BLOCKED_SQUAREOFF"
        assert on_ledger_event(st, LedgerEvent("TRADE_CLOSED", "k", i, fill(side="SELL")), t, i, NO_NEW) == []
    assert st.void_trades == [1, 2, 3] and st.trades_used(3) == 0 and st.trades_left(3) == 4
    later = t + timedelta(minutes=5)
    ev = on_ledger_event(st, LedgerEvent("TRADE_OPENED", "k", 4, fill()), later, 4, NO_NEW)
    assert ev[0].kind == "TRADE_OPENED" and "Trade 1 of 4" in ev[0].message
    for i in range(5, 8):
        on_ledger_event(st, LedgerEvent("TRADE_OPENED", "k", i, fill()), later, i, NO_NEW)
    assert st.trades_used(7) == 4 and not st.lockout
    ev = on_ledger_event(st, LedgerEvent("TRADE_OPENED", "k8", 8, fill()), later, 8, NO_NEW)
    assert ev[0].kind == "TRADE_CAP_SQUAREOFF" and "Trade 5 of 4" in ev[0].message
    ev = on_ledger_event(st, LedgerEvent("TRADE_CLOSED", "k", 7, fill(side="SELL")), later, 8, NO_NEW)
    assert [e.kind for e in ev] == ["TRADE_CLOSED", "TRADE_CAP_LOCKOUT"]


def test_new_position_during_lockout_cooldown_or_after_cutoff_is_squared_off():
    st = state()
    st.lockout, st.lockout_reason = True, "loss limit"
    t = ist(2026, 11, 2, 10, 0)
    ev = on_ledger_event(st, LedgerEvent("TRADE_OPENED", "k", 1, fill()), t, 1, NO_NEW)
    assert ev[0].kind == "BLOCKED_SQUAREOFF" and "lockout" in ev[0].reason
    st2 = state()
    st2.cooldown_until = t + timedelta(minutes=2)
    ev = on_ledger_event(st2, LedgerEvent("POSITION_INCREASED", "k", 1, fill(qty=130)), t, 1, NO_NEW)
    assert ev[0].kind == "BLOCKED_SQUAREOFF" and ev[0].square_off_qty == 130
    st3 = state()
    ev = on_ledger_event(st3, LedgerEvent("TRADE_OPENED", "k", 1, fill()), ist(2026, 11, 2, 15, 11), 1, NO_NEW)
    assert ev[0].kind == "BLOCKED_SQUAREOFF" and "cut-off" in ev[0].reason
    ev = on_ledger_event(st3, LedgerEvent("TRADE_OPENED", "k", 1, fill()), ist(2026, 11, 2, 15, 9), 1, NO_NEW)
    assert ev[0].kind == "TRADE_OPENED"


def test_day_state_round_trips_through_json():
    st = state()
    t = ist(2026, 11, 2, 11, 0)
    run(st, 200_000, t)
    evaluate(st, 133_000, t, True, EXIT_AT)
    st.orders.append({"action": "place", "order_id": "1"})
    d = st.to_dict()
    import json
    back = DayState.from_dict(json.loads(json.dumps(d)))
    assert back.floor == st.floor and back.cooldown_until == st.cooldown_until
    assert back.thresholds.loss_limit == 200_000 and back.orders == st.orders
