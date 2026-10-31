"""'/live lock': the profit lock (and the cool-down after it) sells for real while every other
rule stays dry-run and only alerts, without simulating, so the books match the real account."""

from helpers import FakeBroker, NIFTY, NIFTY2, ist
from test_service import advance, build
from test_service_stops import buy, tick


def lock_only(tmp_path, b):
    svc, tg, _, holder, clock = build(tmp_path, b, now=ist(2026, 11, 2, 9, 5))
    svc.tick()
    reply = svc.handle_command("/live lock", holder["now"])
    assert "DRYRUN, LIVE: lock" in reply and "REAL orders" in reply
    assert svc.store.mode == "dryrun" and svc.store.live_rules == ["lock"]
    holder["now"] = ist(2026, 11, 2, 10, 0)
    return svc, tg, holder, clock


def test_profit_lock_exit_sends_real_orders(tmp_path):
    b = FakeBroker(bids={"NFO:NIFTY26N0525000CE": 119.0})
    svc, tg, holder, clock = lock_only(tmp_path, b)
    buy(svc, b, NIFTY, 6500, 100.0)
    advance(svc, holder, clock)
    tick(svc, NIFTY, 132.0)                            # +2.08L gross: lock arms (test thresholds: +2L, floor +1.334L)
    advance(svc, holder, clock)
    assert svc.state.lock_armed
    tick(svc, NIFTY, 120.0)                            # +1.3L: floor hit
    b.positions[0]["last_price"] = 120.0
    advance(svc, holder, clock)
    assert any("PROFIT LOCK" in t and "Exit orders going out" in t for t in tg.sent)
    assert b.placed and all(not str(o.get("order_id", "")).startswith("DRY") for o in b.placed)
    assert sum(o["quantity"] for o in b.placed) == 6500
    # a buy during the 3-minute cool-down is squared off for real too
    before = len(b.placed)
    buy(svc, b, NIFTY2, 130, 50.0, oid="cool1")
    advance(svc, holder, clock, ticks=2)
    assert [o for o in b.placed[before:] if o["tradingsymbol"] == NIFTY2["tradingsymbol"]]


def test_loss_limit_only_alerts_and_does_not_simulate(tmp_path):
    b = FakeBroker(bids={"NFO:NIFTY26N0525000CE": 69.0})
    svc, tg, holder, clock = lock_only(tmp_path, b)
    buy(svc, b, NIFTY, 6500, 100.0)
    advance(svc, holder, clock)
    tick(svc, NIFTY, 68.0)                             # -2.08L: loss limit
    advance(svc, holder, clock, ticks=3)
    assert any("LOSS LIMIT" in t and "Not sold: only lock is live" in t for t in tg.sent)
    assert b.placed == [] and not [o for o in svc.state.orders if o["action"] == "place"]
    assert svc.ledger.quantities()                     # still open in the books, as in the real account
    # the lockout poll tells once, it does not repeat every poll
    assert sum("Open during lockout" in t for t in tg.sent) <= 1


def test_mode_changes_accepted_after_the_close_not_during_the_session(tmp_path):
    svc, tg, b, holder, clock = build(tmp_path, now=ist(2026, 11, 2, 10, 0))
    svc.tick()
    assert "only accepted before 9:15am or after 3:40pm" in svc.handle_command("/live lock", holder["now"])
    holder["now"] = ist(2026, 11, 2, 15, 45)
    assert "LIVE: lock" in svc.handle_command("/live lock", holder["now"])
    assert "Unknown rule" in svc.handle_command("/live stops", holder["now"])
    assert "Mode is now DRYRUN." == svc.handle_command("/dryrun", holder["now"]) and svc.store.live_rules == []
    assert "Mode is now LIVE." == svc.handle_command("/live", holder["now"]) and svc.store.mode == "live"


def test_rule_of():
    from ta.service import Service
    assert Service.rule_of("profit lock") == "lock"
    assert Service.rule_of("cool-down (120s left)") == "lock"
    assert Service.rule_of("loss limit") == "loss"
    assert Service.rule_of("lockout (loss limit)") == "loss"
    assert Service.rule_of("end of day") == "eod" and Service.rule_of("after the cut-off") == "eod"
    assert Service.rule_of("manual") == "manual" and Service.rule_of("stop hit") == "other"
