"""End-to-end through Service with a FakeBroker and no Kite session."""

from datetime import timedelta

from ta.config import RulePercents
from ta.service import Service
from ta.state import Store, load_day_record
from helpers import Clock, FakeBroker, FakeTelegram, NIFTY, NIFTY2, SENSEX, ist, make_settings

DAY = ist(2026, 11, 2, 9, 0)   # Monday


def build(tmp_path, broker=None, now=DAY, **settings):
    clock = Clock()
    tg = FakeTelegram()
    broker = broker or FakeBroker()
    holder = {"now": now}
    svc = Service(make_settings(tmp_path, **settings), Store(tmp_path), tg, broker, session=None,
                  now_fn=lambda: holder["now"], clock=clock, sleep=clock.sleep)
    return svc, tg, broker, holder, clock


def advance(svc, holder, clock, seconds=2.0, ticks=1):
    for _ in range(ticks):
        holder["now"] = holder["now"] + timedelta(seconds=seconds)
        clock.t += seconds
        svc.tick()
        if svc.sweep_thread is not None:
            svc.sweep_thread.join(timeout=5)


def order_update(inst, qty, price, side="BUY", oid="k1", status="COMPLETE"):
    return {"order_id": oid, "exchange": inst["exchange"], "tradingsymbol": inst["tradingsymbol"], "product": "MIS",
            "transaction_type": side, "filled_quantity": qty, "average_price": price, "status": status}


def test_new_day_reads_capital_and_sends_setup(tmp_path):
    svc, tg, b, holder, clock = build(tmp_path)
    svc.tick()
    assert svc.state is not None and svc.state.thresholds.capital == 20_00_000
    assert svc.store.last_good_capital == 20_00_000
    texts = "\n".join(tg.sent)
    assert "Trading Assistant is running (DRYRUN)" in texts and "Morning setup" in texts and "-₹2.00L" in texts
    assert load_day_record(tmp_path, holder["now"].date())["thresholds"]["loss_limit"] == 200_000


def test_capital_sanity_uses_last_good_value(tmp_path):
    svc, tg, b, holder, clock = build(tmp_path)
    svc.store.last_good_capital = 20_00_000
    b.margins = {"available": {"opening_balance": 5_00_000}}
    svc.tick()
    assert svc.state.thresholds.capital == 20_00_000 and "last good" in svc.state.capital_source
    assert any("implausible" in t for t in tg.sent)


def test_set_guard_refuses_loosening_in_market_hours_unless_forced(tmp_path):
    svc, tg, b, holder, clock = build(tmp_path, now=ist(2026, 11, 2, 10, 0))
    svc.tick()
    assert "LOOSENS" in svc.handle_command("/set loss_limit 250000", holder["now"])
    assert svc.state.thresholds.loss_limit == 200_000
    assert "FORCED" in svc.handle_command("/set loss_limit 250000 force", holder["now"])
    assert svc.state.thresholds.loss_limit == 250_000
    assert "tighten" in svc.handle_command("/set max_trades 3", holder["now"])
    assert svc.state.thresholds.max_trades == 3
    assert svc.state.settings_changes[-1]["name"] == "max_trades"
    assert "Unknown setting" in svc.handle_command("/set foo 1", holder["now"])
    assert "loss_limit = 250000" in svc.handle_command("/set", holder["now"])


def test_set_loosening_allowed_before_open(tmp_path):
    svc, tg, b, holder, clock = build(tmp_path, now=ist(2026, 11, 2, 9, 5))
    svc.tick()
    assert "loosen" in svc.handle_command("/set max_trades 6", holder["now"]) and svc.state.thresholds.max_trades == 6


def test_live_and_dryrun_only_before_open(tmp_path):
    svc, tg, b, holder, clock = build(tmp_path, now=ist(2026, 11, 2, 9, 5))
    svc.tick()
    assert "LIVE" in svc.handle_command("/live", holder["now"]) and svc.store.mode == "live"
    holder["now"] = ist(2026, 11, 2, 9, 20)
    assert "only accepted before" in svc.handle_command("/dryrun", holder["now"]) and svc.store.mode == "live"


def test_dry_run_loss_limit_exit_sweeps_and_books_fills(tmp_path):
    b = FakeBroker(bids={"NFO:NIFTY26N0525000CE": 70.0})
    svc, tg, _, holder, clock = build(tmp_path, b, now=ist(2026, 11, 2, 10, 0))
    svc.tick()
    # buy 6500 @ 100, then the price collapses to 70: open loss 1.95L + charges -> past -2L
    svc.on_order_update(order_update(NIFTY, 6500, 100.0))
    b.add_position(NIFTY, 6500, 100.0, last_price=70.0)
    advance(svc, holder, clock)
    svc.ltp["NFO:NIFTY26N0525000CE"] = 68.0
    advance(svc, holder, clock)
    st = svc.state
    assert st.lockout and st.lockout_reason == "loss limit"
    assert any("LOSS LIMIT" in t for t in tg.sent)
    # the dry-run broker "sold" it: a DRY order is recorded, the ledger shows the sale, positions flat
    placed = [o for o in st.orders if o["action"] == "place"]
    assert placed and placed[0]["order_id"].startswith("DRY") and placed[0]["qty"] == 3510
    assert b.placed == []                       # nothing reached the real broker
    assert svc.ledger.quantities() == {}
    assert svc.ledger.trades_closed == 1
    assert any("FLAT" in t for t in tg.sent)


def test_live_mode_sends_real_orders(tmp_path):
    b = FakeBroker(bids={"NFO:NIFTY26N0525000CE": 70.0})
    svc, tg, _, holder, clock = build(tmp_path, b, now=ist(2026, 11, 2, 9, 5))
    svc.tick()
    svc.handle_command("/live", holder["now"])
    holder["now"] = ist(2026, 11, 2, 10, 0)
    svc.on_order_update(order_update(NIFTY, 6500, 100.0))
    b.add_position(NIFTY, 6500, 100.0, last_price=68.0)
    advance(svc, holder, clock, ticks=2)
    assert [o["quantity"] for o in b.placed] == [3510, 2990]
    assert all(o["price"] == 66.5 for o in b.placed)


def test_pause_auto_exit_and_kill_stop_orders_but_keep_alerts(tmp_path):
    b = FakeBroker()
    svc, tg, _, holder, clock = build(tmp_path, b, now=ist(2026, 11, 2, 10, 0))
    svc.tick()
    svc.handle_command("/pause", holder["now"])
    svc.on_order_update(order_update(NIFTY, 6500, 100.0))
    b.add_position(NIFTY, 6500, 100.0, last_price=60.0)
    advance(svc, holder, clock, ticks=2)
    assert svc.state.lockout and any("AUTO-EXIT PAUSED" in t for t in tg.sent)
    assert not [o for o in svc.state.orders if o["action"] == "place"]
    # a position open during lockout is not squared off either while paused... until /resume
    svc.handle_command("/resume", holder["now"])
    svc.handle_command("/kill", holder["now"])
    advance(svc, holder, clock, ticks=2)
    assert not [o for o in svc.state.orders if o["action"] == "place"]
    assert svc.state.killed


def test_lockout_poll_squares_off_new_position_and_pause_squareoff_stops_it(tmp_path):
    b = FakeBroker()
    svc, tg, _, holder, clock = build(tmp_path, b, now=ist(2026, 11, 2, 10, 0))
    svc.tick()
    svc.handle_command("/exitall", holder["now"])
    advance(svc, holder, clock)
    assert svc.state.lockout
    b.add_position(NIFTY2, 130, 50.0)
    advance(svc, holder, clock, ticks=2)
    placed = [o for o in svc.state.orders if o["action"] == "place"]
    assert placed and placed[-1]["symbol"] == NIFTY2["tradingsymbol"] and placed[-1]["qty"] == 130
    assert any("Squaring off" in t for t in tg.sent)
    # now pause square-off: a new position is only reported
    svc.handle_command("/pause_squareoff", holder["now"])
    before = len([o for o in svc.state.orders if o["action"] == "place"])
    b.add_position(SENSEX, 20, 400.0)
    advance(svc, holder, clock, ticks=2)
    assert len([o for o in svc.state.orders if o["action"] == "place"]) == before


def test_fifth_trade_is_squared_off_and_fourth_close_locks(tmp_path):
    b = FakeBroker()
    svc, tg, _, holder, clock = build(tmp_path, b, now=ist(2026, 11, 2, 10, 0))
    svc.tick()
    syms = [NIFTY, NIFTY2, SENSEX]
    for i, inst in enumerate(syms):
        svc.on_order_update(order_update(inst, 65 if inst is not SENSEX else 20, 100.0, oid=f"b{i}"))
        svc.on_order_update(order_update(inst, 65 if inst is not SENSEX else 20, 101.0, side="SELL", oid=f"s{i}"))
    advance(svc, holder, clock)
    assert svc.ledger.trades_opened == 3 and not svc.state.lockout
    svc.on_order_update(order_update(NIFTY, 65, 100.0, oid="b4"))
    advance(svc, holder, clock)
    assert svc.ledger.trades_opened == 4 and not svc.state.lockout
    b.add_position(NIFTY2, 65, 100.0)
    svc.on_order_update(order_update(NIFTY2, 65, 100.0, oid="b5"))
    advance(svc, holder, clock)
    assert any("Trade 5 of 4" in t for t in tg.sent)
    assert [o for o in svc.state.orders if o["action"] == "place" and o["symbol"] == NIFTY2["tradingsymbol"]]
    svc.on_order_update(order_update(NIFTY, 65, 99.0, side="SELL", oid="s4"))
    advance(svc, holder, clock)
    assert svc.state.lockout and svc.state.lockout_reason == "trade cap"


def test_restart_restores_day_and_status_reports(tmp_path):
    b = FakeBroker()
    svc, tg, _, holder, clock = build(tmp_path, b, now=ist(2026, 11, 2, 10, 0))
    svc.tick()
    svc.handle_command("/set max_trades 2", holder["now"])
    svc.state.exiting = True
    svc._persist_force = True
    svc.dirty = True
    clock.t += 2
    svc._persist()
    svc2, tg2, _, holder2, clock2 = build(tmp_path, b, now=ist(2026, 11, 2, 10, 5))
    svc2.tick()
    assert svc2.state.thresholds.max_trades == 2 and not svc2.state.exiting
    assert any("Restarted during an exit sweep" in t for t in tg2.sent)
    status = svc2.handle_command("/status", holder2["now"])
    assert "Net" in status and "Trades 0/2" in status


def test_eod_exit_and_summary(tmp_path):
    b = FakeBroker()
    svc, tg, _, holder, clock = build(tmp_path, b, now=ist(2026, 11, 2, 15, 5))
    svc.tick()
    svc.on_order_update(order_update(NIFTY, 65, 100.0))
    b.add_position(NIFTY, 65, 100.0, last_price=102.0)
    advance(svc, holder, clock, seconds=60, ticks=2)   # 15:07
    assert svc.state.lockout and svc.state.lockout_reason == "end of day"
    assert svc.ledger.quantities() == {}
    holder["now"] = ist(2026, 11, 2, 15, 31)
    svc.tick()
    assert svc.state.summary_sent and any("End of day" in t for t in tg.sent)


def test_weekend_does_nothing(tmp_path):
    svc, tg, b, holder, clock = build(tmp_path, now=ist(2026, 11, 1, 10, 0))   # Sunday
    svc.tick()
    assert svc.state is None and tg.sent == []
    assert "No session today" in svc.handle_command("/status", holder["now"])


def test_default_settings_have_no_trade_cap(tmp_path):
    svc, tg, b, holder, clock = build(tmp_path, now=ist(2026, 11, 2, 9, 5), percents=RulePercents())
    svc.tick()
    assert any("Trades: no cap, risk/trade" in t for t in tg.sent)
    assert "Trades 0 taken" in svc.handle_command("/status", holder["now"])


def test_slippage_measured_against_last_tick_and_bid_at_send(tmp_path):
    b = FakeBroker(bids={"NFO:NIFTY26N0525000CE": 70.0})
    svc, tg, _, holder, clock = build(tmp_path, b, now=ist(2026, 11, 2, 9, 5))
    svc.tick()
    svc.handle_command("/live", holder["now"])
    holder["now"] = ist(2026, 11, 2, 10, 0)
    # your BUY: the reference is the last tick when Kite first reports the order
    svc.on_ticks([{"instrument_token": NIFTY["instrument_token"], "last_price": 99.5}])
    clock.t += 0.5
    svc.on_order_update(order_update(NIFTY, 6500, 100.0, oid="mine"))
    b.add_position(NIFTY, 6500, 100.0, last_price=68.0)
    svc.on_ticks([{"instrument_token": NIFTY["instrument_token"], "last_price": 68.0}])
    advance(svc, holder, clock, ticks=2)             # loss limit -> the TA sells at 66.5 against a 70 bid
    mine = svc.state.slippage[0]
    assert (mine["order_id"], mine["ref_kind"], mine["points"], mine["ref_age_s"]) == ("mine", "ltp", 0.5, 0.5)
    ta_order = next(o for o in svc.state.orders if o.get("action") == "place")
    assert ta_order["bid_at_send"] == 70.0
    svc.on_order_update(order_update(NIFTY, ta_order["qty"], 66.5, side="SELL", oid=ta_order["order_id"]))
    advance(svc, holder, clock)
    exit_rec = next(r for r in svc.state.slippage if r["order_id"] == ta_order["order_id"])
    assert exit_rec["ref_kind"] == "bid_at_send" and exit_rec["points"] == 3.5


def test_watchlist_follows_spot(tmp_path):
    b = FakeBroker()
    b.spot = {"NSE:NIFTY 50": 24990.0, "BSE:SENSEX": 82010.0}
    svc, tg, _, holder, clock = build(tmp_path, b, now=ist(2026, 11, 2, 9, 5))
    svc.tick()
    assert svc.watch_tokens == {NIFTY["instrument_token"], NIFTY2["instrument_token"], SENSEX["instrument_token"]}
    b.spot = {}
    advance(svc, holder, clock, seconds=61)   # spot read empty: keep the last list
    assert len(svc.watch_tokens) == 3


class RecordingSession:
    """Just enough KiteSession for the loop: a valid token and a subscribe() that records."""
    def __init__(self):
        self.token, self.last_token_check, self.subscribed = "t", 10**12, set()
        self.ticker_connected, self.last_tick_at = True, 0.0

    def subscribe(self, tokens):
        self.subscribed = set(tokens)


def test_subscription_is_open_positions_plus_watchlist(tmp_path):
    b = FakeBroker()
    b.spot = {"BSE:SENSEX": 82010.0}                     # NIFTY spot unavailable: only SENSEX is watched
    b.add_position(NIFTY2, 65, 50.0)
    session = RecordingSession()
    holder = {"now": ist(2026, 11, 2, 10, 0)}
    clock = Clock()
    svc = Service(make_settings(tmp_path), Store(tmp_path), FakeTelegram(), b, session=session,
                  now_fn=lambda: holder["now"], clock=clock, sleep=clock.sleep)
    svc.instruments = {}
    svc._load_instruments()
    svc.tick()
    assert session.subscribed == {NIFTY2["instrument_token"], SENSEX["instrument_token"]}


def test_kill_before_the_session_applies_to_the_next_day(tmp_path):
    svc, tg, b, holder, clock = build(tmp_path, now=ist(2026, 10, 31, 11, 0))      # Saturday
    svc.tick()
    assert "next trading day" in svc.handle_command("/kill", holder["now"])
    assert svc.store.kill_pending
    # a restart over the weekend keeps it
    svc2, tg2, _, holder2, clock2 = build(tmp_path, b, now=ist(2026, 11, 2, 9, 0))  # Monday
    svc2.tick()
    assert svc2.state.killed and not svc2.store.kill_pending
    assert any("sent before today's session" in t for t in tg2.sent)
    assert "KILLED" in svc2.handle_command("/status", holder2["now"])


def test_kill_survives_a_restart_the_same_day(tmp_path):
    svc, tg, b, holder, clock = build(tmp_path, now=ist(2026, 11, 2, 10, 0))
    svc.tick()
    assert "even after a restart" in svc.handle_command("/kill", holder["now"])
    svc.dirty = True
    clock.t += 10
    svc._persist()
    svc2, tg2, _, holder2, clock2 = build(tmp_path, b, now=ist(2026, 11, 2, 10, 5))
    svc2.tick()
    assert svc2.state.killed


class RejectingSession:
    """A KiteSession whose token file is yesterday's: every check fails."""
    def __init__(self):
        self.token, self.last_token_check = "", 0.0
        self.kite = type("K", (), {"profile": staticmethod(lambda: None)})()
        self.checks = 0

    def ensure_session(self, verify=None):
        self.checks += 1
        return False, "token rejected by Kite; waiting for a new token file"


def test_dead_token_is_quiet_before_the_cron_and_alerts_every_15_minutes_after(tmp_path):
    session = RejectingSession()
    tg = FakeTelegram()
    holder = {"now": ist(2026, 11, 2, 8, 0)}
    clock = Clock()
    svc = Service(make_settings(tmp_path), Store(tmp_path), tg, FakeBroker(), session=session,
                  now_fn=lambda: holder["now"], clock=clock, sleep=clock.sleep)
    advance(svc, holder, clock, seconds=30, ticks=99)           # 08:00 -> 08:49:30
    assert session.checks >= 90
    assert tg.sent == []                                         # expected until the cron runs
    advance(svc, holder, clock, seconds=30, ticks=1)             # 08:50:00
    assert len(tg.sent) == 1 and "token rejected" in tg.sent[0] and "URGENT" in tg.sent[0]
    advance(svc, holder, clock, seconds=30, ticks=28)            # +14 min
    assert len(tg.sent) == 1
    advance(svc, holder, clock, seconds=30, ticks=2)             # +15 min
    assert len(tg.sent) == 2
