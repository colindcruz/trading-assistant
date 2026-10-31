from datetime import date

from ta import brake
from ta.config import RulePercents, Thresholds
from ta.state import save_day_record
from helpers import ist
from test_service import build

MON, TUE, WED, THU, FRI = (date(2026, 11, 2 + i) for i in range(5))
NEXT_MON = date(2026, 11, 9)


def test_counts_earlier_days_of_this_week_only():
    assert brake.past_trading_days_this_week(WED) == [MON, TUE]
    hit = {"loss_limit_hit": True}
    assert brake.count_max_loss_days(WED, {MON: hit, TUE: hit}, MON) == (2, [])
    assert brake.count_max_loss_days(WED, {MON: hit, TUE: {}}, MON) == (1, [])
    # Friday's second max-loss day does not carry into the next week
    assert brake.count_max_loss_days(NEXT_MON, {}, MON) == (0, [])


def test_a_missing_record_counts_from_the_first_day_on():
    hit = {"loss_limit_hit": True}
    assert brake.count_max_loss_days(WED, {MON: hit, TUE: None}, MON) == (2, ["2026-11-03"])
    assert brake.count_max_loss_days(WED, {MON: None, TUE: hit}, TUE) == (1, [])   # before the TA existed


def test_levels_and_halving():
    assert [brake.level(n) for n in (0, 1, 2, 3, 4)] == ["off", "off", "half", "week_over", "week_over"]
    th = brake.halve(Thresholds.from_capital(20_00_000, RulePercents()))
    assert (th.loss_limit, th.loss_warn1, th.loss_warn2) == (100_000, 50_000, 75_000)
    assert (th.lock_arm, th.lock_floor, th.trail_start, th.trail_distance) == (125_000, 83_300, 156_000, 42_500)
    assert th.reentry_floor_step == 25_000 and th.max_trades == 0 and th.cooldown_minutes == 3


def test_clean_weeks_streak():
    good = brake.week_summary([{"last_net": 50_000}, {"last_net": 20_000}])
    braked = brake.week_summary([{"last_net": 90_000, "brake": "half"}])
    forced = brake.week_summary([{"last_net": 90_000, "settings_changes": [{"forced": True}]}])
    empty = brake.week_summary([])
    assert good == {"net": 70_000, "max_loss_days": 0, "brake": False, "overrides": 0, "days": 2}
    assert brake.clean_streak([good, good, good, good]) == 4
    assert brake.clean_streak([good, good, braked, good]) == 1
    assert brake.clean_streak([good, forced]) == 0 and brake.clean_streak([empty, good]) == 1


def seed(tmp_path, days):
    for d in days:
        save_day_record(tmp_path, {"date": d.isoformat(), "loss_limit_hit": True, "last_net": -200_000})


def test_service_halves_after_two_max_loss_days(tmp_path):
    seed(tmp_path, [MON, TUE])
    svc, tg, b, holder, clock = build(tmp_path, now=ist(2026, 11, 4, 9, 5))
    svc.store.first_day = "2026-11-02"
    svc.tick()
    st = svc.state
    assert st.brake == "half" and st.brake_days == 2 and st.thresholds.loss_limit == 100_000
    assert any("WEEKLY BRAKE: 2 max-loss days" in t and "1,300 NIFTY / 500 SENSEX" in t for t in tg.sent)
    assert any("Weekly brake: HALF (2 max-loss days earlier this week)" in t for t in tg.sent)
    assert "Weekly brake: HALF" in svc.handle_command("/status", holder["now"])


def test_service_third_day_ends_the_week(tmp_path):
    seed(tmp_path, [MON, TUE, WED])
    svc, tg, b, holder, clock = build(tmp_path, now=ist(2026, 11, 5, 9, 5))
    svc.store.first_day = "2026-11-02"
    svc.tick()
    assert svc.state.brake == "week_over" and svc.state.lockout
    assert "weekly brake" in svc.state.lockout_reason
    assert any("No trading until Monday" in t for t in tg.sent)


def test_first_ever_day_sets_first_day_and_no_brake(tmp_path):
    svc, tg, b, holder, clock = build(tmp_path, now=ist(2026, 11, 4, 9, 5))   # Wednesday, nothing before
    svc.tick()
    assert svc.store.first_day == "2026-11-04" and svc.state.brake == "off"


def test_friday_summary_carries_the_week_line(tmp_path):
    for d in (MON, TUE, WED, THU):
        save_day_record(tmp_path, {"date": d.isoformat(), "last_net": 50_000})
    svc, tg, b, holder, clock = build(tmp_path, now=ist(2026, 11, 6, 15, 31))   # Friday after the close
    svc.store.first_day = "2026-11-02"
    svc.tick()
    week = [t for t in tg.sent if "Week: net" in t]
    assert week and "Week: net +₹2.00L" in week[0] and "Clean weeks in a row: 1 of 4" in week[0]
    assert "Eligible" not in week[0]
    svc2, tg2, _, holder2, _ = build(tmp_path / "thu", now=ist(2026, 11, 5, 15, 31))
    svc2.tick()
    assert not any("Week: net" in t for t in tg2.sent)
