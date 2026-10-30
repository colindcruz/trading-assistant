"""
The Trading Assistant service: wires Kite, the ledger, the rules and Telegram together
and runs the one-second loop.

Threads:
  main        this loop (rules, polls, commands, persistence)
  ticker      KiteTicker (twisted): on_ticks -> self.ltp, on_order_update -> self.updates queue
  telegram    long-polls getUpdates into self.commands queue
  sweep       one at a time: runs the exit engine so the loop stays responsive to /kill

Everything that decides is pure (rules.py, ledger.py); this file only moves data
between the broker, those modules and the user.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime, timedelta

from . import rules
from .broker import Broker, DryRunBroker, KiteBroker
from .clock import at_or_after, before, fmt_hhmm, in_market_hours, is_trading_day, now_ist, rupees
from .config import Settings, Thresholds
from .exit_engine import TAG_PREFIX, ExitEngine, SweepResult
from .exit_logic import LONG, build_instrument_index, select_index_options
from .ledger import ContractState, Fill, Ledger, fill_from_order_update, fill_from_trade
from .rules import DayState, Event
from .state import Store, load_day_record, save_day_record
from .telegram import Notifier, Telegram, classify_change, parse_command

log = logging.getLogger("ta")


class SwitchingBroker:
    """Delegates to the live or dry-run broker depending on the current mode, so the
    exit engine never has to be rebuilt when /live or /dryrun is used."""

    def __init__(self, live: Broker, dry: DryRunBroker, mode_fn):
        self.live, self.dry, self.mode_fn = live, dry, mode_fn

    def __getattr__(self, name):
        target = self.live if self.mode_fn() == "live" else self.dry
        return getattr(target, name)


class Service:
    def __init__(self, settings: Settings, store: Store, telegram: Telegram, broker: Broker,
                 session=None, now_fn=now_ist, clock=time.time, sleep=time.sleep):
        self.s = settings
        self.store = store
        self.tg = telegram
        self.session = session                     # KiteSession, or None in tests
        self.now_fn = now_fn
        self.clock = clock
        self.sleep = sleep
        self.dry = DryRunBroker(broker, on_action=self._dry_action, clock=clock)
        self.broker = SwitchingBroker(broker, self.dry, lambda: self.store.mode)
        self.notifier = Notifier(telegram, is_muted=lambda: bool(self.state and self.state.pause_notifications), clock=clock)

        self.state: DayState | None = None
        self.ledger: Ledger | None = None
        self.instruments: dict = {}
        self.token_to_key: dict[int, str] = {}
        self.engine: ExitEngine | None = None

        self.ltp: dict[str, float] = {}             # "EX:SYM" -> price, from ticks
        self.ltp_lock = threading.Lock()
        self.updates: queue.Queue = queue.Queue()  # order updates from the ticker thread
        self.commands: queue.Queue = queue.Queue()
        self.order_filled_seen: dict[str, int] = {}

        self.sweep_thread: threading.Thread | None = None
        self.allowed_keys: set[str] = set()         # positions allowed to stay open during a trade-cap lockout
        self.last_positions_poll = 0.0
        self.last_positions_ok = 0.0
        self.last_trades_reconcile = 0.0
        self.last_save = 0.0
        self.last_pause_reminder = 0.0
        self.dirty = False
        self.rows: list[dict] = []
        self.shorts: list[dict] = []
        self.stop = threading.Event()

    # ================================================================ lifecycle
    def run(self) -> None:
        problems = self.s.validate()
        if problems:
            for p in problems:
                log.error("Config: %s", p)
            raise SystemExit("Fix .env: " + "; ".join(problems))
        log.info("Trading Assistant starting, mode=%s, state_dir=%s", self.store.mode, self.s.state_dir)
        threading.Thread(target=self._telegram_loop, name="telegram", daemon=True).start()
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                log.exception("Loop error: %s", exc)
                self.notifier.alert(f"Loop error: {exc}", urgent=True, kind="loop_error")
                self.sleep(5)
            self.sleep(1.0)

    def tick(self) -> None:
        now = self.now_fn()
        self._handle_commands(now)
        if not is_trading_day(now.date()):
            if self.state is not None and self.state.date != now.date().isoformat():
                self.state = None
            return
        if not self._ensure_session(now):
            return
        if self.state is None or self.state.date != now.date().isoformat():
            self._start_day(now)
            if self.state is None:
                return
        self._pre_open_messages(now)
        self._drain_order_updates(now)
        self._poll_positions(now)
        self._reconcile_trades(now)
        if in_market_hours(now, self.s.market_open, self.s.market_close) or at_or_after(now, self.s.market_close):
            self._run_rules(now)
        self._heartbeat(now)
        self._eod_summary(now)
        self._persist()

    # ================================================================ session / day
    def _ensure_session(self, now: datetime) -> bool:
        if self.session is None:
            return True
        if self.clock() - self.session.last_token_check < self.s.token_check_seconds and self.session.token:
            return True
        self.session.last_token_check = self.clock()
        ok, msg = self.session.ensure_session(verify=self.session.kite.profile)
        if not ok:
            if before(now, self.s.pre_open) or in_market_hours(now, self.s.market_open, self.s.market_close):
                self.notifier.alert(f"Kite session problem: {msg}", urgent=True, kind="session")
            return False
        if msg != "ok":
            log.info("Session: %s", msg)
            self._load_instruments()
        return True

    def _load_instruments(self) -> None:
        try:
            rows = self.broker.instruments("NFO") + self.broker.instruments("BFO")
        except Exception as exc:  # noqa: BLE001
            log.error("instruments() failed: %s", exc)
            return
        self.instruments = build_instrument_index(rows)
        self.token_to_key = {m["instrument_token"]: f"{ex}:{sym}" for (ex, sym), m in self.instruments.items()}
        if self.engine is not None:
            self.engine.instruments = self.instruments
        log.info("Instruments loaded: %d NIFTY/SENSEX option contracts", len(self.instruments))

    def _start_day(self, now: datetime) -> None:
        if not self.instruments:
            self._load_instruments()
            if not self.instruments:
                return
        self.dry.reset_day()
        self.ledger = Ledger(self.s.charges)
        self.allowed_keys = set()
        self.order_filled_seen = {}
        record = load_day_record(self.s.state_dir, now.date())
        if record:
            self.state = DayState.from_dict(record)
            self.state.mode = self.store.mode
            if self.state.exiting:
                self.state.exiting = False
                self.state.notes.append(f"{now:%H:%M:%S} restarted mid-sweep; positions re-read from Kite")
                self.notifier.alert("Restarted during an exit sweep. Re-reading positions; the lockout poll will finish the job.",
                                    urgent=True)
            log.info("Day record restored for %s", now.date())
        else:
            capital, source = self._read_capital()
            th = Thresholds.from_capital(capital, self.s.percents)
            self.state = DayState(date=now.date().isoformat(), thresholds=th, mode=self.store.mode, capital_source=source)
            log.info("New day %s: capital %s (%s)", now.date(), capital, source)
        self.engine = ExitEngine(self.broker, self.s, self.instruments, self._engine_notify, self._record_order,
                                 clock=self.clock, sleep=self.sleep, now_fn=self.now_fn)
        self.dirty = True
        self._reconcile_trades(now, force=True)

    def _read_capital(self) -> tuple[float, str]:
        if self.s.capital_override > 0:
            return self.s.capital_override, "CAPITAL_OVERRIDE"
        last = self.store.last_good_capital
        try:
            m = self.broker.margins_equity()
            opening = float((m.get("available") or {}).get("opening_balance") or 0)
        except Exception as exc:  # noqa: BLE001
            log.error("margins() failed: %s", exc)
            opening = 0.0
        if opening <= 0 or (last > 0 and abs(opening - last) / last * 100 > self.s.capital_sanity_pct):
            if last > 0:
                self.notifier.alert(f"Capital read failed or implausible ({rupees(opening)}); using last good {rupees(last)}.", urgent=True)
                return last, f"last good (read {opening})"
            if opening <= 0:
                self.notifier.alert("Capital read failed and no last good value. Set CAPITAL_OVERRIDE or fix Kite.", urgent=True)
                return 0.0, "none"
        self.store.last_good_capital = opening
        return opening, "opening_balance"

    # ================================================================ data in
    def on_ticks(self, ticks: list[dict]) -> None:
        """Ticker thread."""
        with self.ltp_lock:
            for t in ticks:
                key = self.token_to_key.get(t.get("instrument_token"))
                price = t.get("last_price")
                if key and price:
                    self.ltp[key] = float(price)

    def on_order_update(self, data: dict) -> None:
        """Ticker thread."""
        self.updates.put(dict(data))

    def _drain_order_updates(self, now: datetime) -> None:
        while True:
            try:
                data = self.updates.get_nowait()
            except queue.Empty:
                return
            self._apply_order_update(data, now)

    def _apply_order_update(self, data: dict, now: datetime) -> None:
        oid = str(data.get("order_id") or "")
        if data.get("exchange") not in ("NFO", "BFO") or not oid:
            return
        key = (data.get("exchange"), data.get("tradingsymbol"))
        if key not in self.instruments:
            return
        prev = self.order_filled_seen.get(oid, 0)
        fill = fill_from_order_update(data, prev, now)
        if fill is None:
            return
        self.order_filled_seen[oid] = prev + fill.quantity
        self._apply_fill(fill, now)

    def _apply_fill(self, fill: Fill, now: datetime) -> None:
        if self.ledger is None or self.state is None:
            return
        for ev in self.ledger.apply(fill):
            log.info("Ledger: %s %s trade=%s", ev.kind, ev.key, ev.trade_no)
            for event in rules.on_ledger_event(self.state, ev, now, self.ledger.trades_opened, self.s.no_new_time):
                self._act(event, now)
            self.dirty = True

    def _reconcile_trades(self, now: datetime, force: bool = False) -> None:
        """The trades API is exact; apply anything the socket missed (idempotent)."""
        if self.ledger is None:
            return
        if not force and self.clock() - self.last_trades_reconcile < 60:
            return
        self.last_trades_reconcile = self.clock()
        try:
            trades = self.broker.trades()
        except Exception as exc:  # noqa: BLE001
            log.warning("trades() failed: %s", exc)
            return
        fills = []
        for t in trades:
            if t.get("exchange") not in ("NFO", "BFO") or (t.get("exchange"), t.get("tradingsymbol")) not in self.instruments:
                continue
            try:
                fills.append(fill_from_trade(t))
            except (KeyError, ValueError, TypeError) as exc:
                log.warning("Bad trade row %s: %s", t.get("trade_id"), exc)
        for f in sorted(fills, key=lambda x: (x.ts, x.fill_id)):
            if f.fill_id not in self.ledger.seen_fill_ids:
                # a socket delta for the same order may already be in; count by order fill totals
                self._apply_fill(f, now)

    def _poll_positions(self, now: datetime) -> None:
        if self.clock() - self.last_positions_poll < self.s.positions_poll_seconds:
            return
        self.last_positions_poll = self.clock()
        try:
            net = self.broker.positions_net()
        except Exception as exc:  # noqa: BLE001
            log.warning("positions() failed: %s", exc)
            return
        self.last_positions_ok = self.clock()
        rows, _unknown = select_index_options(net, self.instruments)
        self.rows = [r for r in rows if r["side"] == LONG]
        self.shorts = [r for r in rows if r["side"] != LONG]
        # subscribe to what is open; fall back to positions' last_price for LTP
        if self.session is not None:
            self.session.subscribe({r["instrument_token"] for r in rows})
        with self.ltp_lock:
            for r in rows:
                self.ltp.setdefault(f"{r['exchange']}:{r['tradingsymbol']}", r["last_price"])
        self._adopt_unknown_positions(now)
        self._enforce_lockout(now)

    def _adopt_unknown_positions(self, now: datetime) -> None:
        """A long position Kite reports that the ledger has never seen (carried overnight,
        or opened before the TA started and missing from trades()) is adopted at its
        average price so it counts in open P&L, without a trade number."""
        if self.ledger is None:
            return
        for r in self.rows:
            c = self.ledger.contracts.get(r["key"])
            if c is None:
                self.ledger.contracts[r["key"]] = ContractState(r["key"], r["exchange"], r["tradingsymbol"], r["product"],
                                                                 qty=r["quantity"], avg_cost=r["average_price"])
                log.info("Adopted %s x%d @ %s (not in ledger)", r["tradingsymbol"], r["quantity"], r["average_price"])
                self.dirty = True

    # ================================================================ rules
    def _ltp_by_key(self) -> dict[str, float]:
        with self.ltp_lock:
            return dict(self.ltp)

    def net_pnl(self) -> float:
        return self.ledger.net_pnl(self._ltp_by_key()) if self.ledger else 0.0

    def _run_rules(self, now: datetime) -> None:
        if self.state is None or self.ledger is None:
            return
        net = self.net_pnl()
        has_open = bool(self.rows)
        for event in rules.evaluate(self.state, net, now, has_open, self.s.exit_all_time):
            self._act(event, now)

    def _act(self, event: Event, now: datetime) -> None:
        st = self.state
        log.info("Event %s: %s", event.kind, event.message)
        self.dirty = True
        if event.kind == "TRADE_CAP_LOCKOUT":
            self.allowed_keys = {r["key"] for r in self.rows}
        urgent = event.urgent
        text = event.message
        if event.exit_all:
            if st.killed or st.pause_auto_exit:
                # These positions are now the trader's responsibility: the lockout poll
                # must not sell them later. Anything NEW that appears is still squared off.
                self.allowed_keys = {r["key"] for r in self.rows}
                text += ("\nKILLED: no orders will be sent. Exit manually." if st.killed
                         else "\nAUTO-EXIT PAUSED: no orders will be sent. Exit manually or /resume.")
            else:
                self.allowed_keys = set()
                self._start_sweep(lambda: self.engine.exit_all(event.reason), event.reason)
                text += "\nExit orders going out."
        elif event.square_off:
            if st.killed:
                text += "\nKILLED: not squaring off."
            elif st.pause_squareoff:
                text += "\nSQUARE-OFF PAUSED: left open."
            else:
                self._start_sweep(lambda: self.engine.square_off(event.square_off, event.reason, event.square_off_qty), event.reason)
        self.notifier.alert(text, urgent=urgent, kind=event.kind if event.kind.endswith("SQUAREOFF") else None)

    def _enforce_lockout(self, now: datetime) -> None:
        """Positions poll path: anything open that should not be gets squared off. This
        is the retry for an incomplete sweep and the backstop for a missed fill."""
        st = self.state
        if st is None or not self.rows or st.exiting or st.killed or st.pause_squareoff:
            return
        reason = st.blocked_reason(now, self.s.no_new_time)
        if reason is None:
            return
        offenders = [r for r in self.rows if r["key"] not in self.allowed_keys]
        if not offenders:
            return
        names = ", ".join(f"{r['tradingsymbol']} x{r['quantity']}" for r in offenders)
        self.notifier.alert(f"Open during {reason}: {names}. Squaring off.", urgent=True, kind="lockout_poll")
        self._start_sweep(lambda: self.engine.exit_all(f"{reason} square-off") if len(offenders) == len(self.rows)
                          else self._square_off_many(offenders, reason), reason)

    def _square_off_many(self, rows: list[dict], reason: str) -> SweepResult:
        last = SweepResult(reason=reason, nothing_to_do=True)
        for r in rows:
            last = self.engine.square_off(r["key"], reason)
        return last

    # ================================================================ sweeps
    def _start_sweep(self, fn, reason: str) -> None:
        if self.sweep_thread is not None and self.sweep_thread.is_alive():
            log.info("Sweep already running; %s will be retried by the positions poll", reason)
            return
        self.state.exiting = True
        self.dirty = True

        def run():
            try:
                result = fn()
            except Exception as exc:  # noqa: BLE001
                log.exception("Sweep failed: %s", exc)
                self.notifier.alert(f"EXIT SWEEP FAILED ({reason}): {exc}. Exit manually.", urgent=True)
                result = None
            finally:
                self.state.exiting = False
                self.dirty = True
            if result is not None and not result.already_running and not result.nothing_to_do:
                self._ingest_dry_fills(result)
                self.notifier.alert(f"Exit ({reason}): {result.summary()}", urgent=not result.flat)
            self.last_positions_poll = 0.0   # poll right away

        self.sweep_thread = threading.Thread(target=run, name="sweep", daemon=True)
        self.sweep_thread.start()

    def _ingest_dry_fills(self, result: SweepResult) -> None:
        """Dry-run orders never reach Kite, so their fills never arrive on the socket."""
        now = self.now_fn()
        for o in result.placed:
            if not str(o.get("order_id", "")).startswith("DRY"):
                continue
            fill = Fill(fill_id=f"{o['order_id']}:dry", order_id=o["order_id"], exchange=o["exchange"],
                        tradingsymbol=o["symbol"], product=o["product"], side="SELL", quantity=int(o["qty"]),
                        price=float(o.get("avg_price") or o["price"]), ts=now)
            self._apply_fill(fill, now)

    def _engine_notify(self, text: str, urgent: bool = False) -> None:
        self.notifier.alert(text, urgent=urgent)

    def _record_order(self, entry: dict) -> None:
        if self.state is not None:
            self.state.orders.append({k: v for k, v in entry.items() if k != "placed_at"})
            self.dirty = True

    def _dry_action(self, msg: str) -> None:
        log.info("[DRY RUN] %s", msg)
        if self.state is not None:
            self.state.notes.append(f"{self.now_fn():%H:%M:%S} DRY RUN {msg}")

    # ================================================================ messages
    def _pre_open_messages(self, now: datetime) -> None:
        st = self.state
        if not st.guardian_check_sent and at_or_after(now, self.s.guardian_check_time):
            st.guardian_check_sent = True
            self.dirty = True
            self.notifier.alert(f"Trading Assistant is running ({self.store.mode.upper()}). "
                                f"Token OK, ticker {'connected' if self.session is None or self.session.ticker_connected else 'connecting'}. "
                                f"pnl-monitor is replaced by this service.")
        if not st.setup_sent and (at_or_after(now, self.s.guardian_check_time) or at_or_after(now, self.s.pre_open)):
            st.setup_sent = True
            self.dirty = True
            self.notifier.alert(self._setup_text())

    def _setup_text(self) -> str:
        th = self.state.thresholds
        return (f"Morning setup ({self.state.date}) — {self.store.mode.upper()}\n"
                f"Capital {rupees(th.capital)} ({self.state.capital_source})\n"
                f"Loss: warn {rupees(-th.loss_warn1)} / {rupees(-th.loss_warn2)}, limit {rupees(-th.loss_limit)}\n"
                f"Lock: arms {rupees(th.lock_arm)}, floor {rupees(th.lock_floor)}; trail from {rupees(th.trail_start)}, "
                f"distance {rupees(th.trail_distance)}\n"
                f"Trades: max {th.max_trades}, risk/trade {rupees(th.reentry_floor_step)}; cool-down {th.cooldown_minutes} min\n"
                f"Exit all {fmt_hhmm(self.s.exit_all_time)}, no new after {fmt_hhmm(self.s.no_new_time)}")

    def status_text(self, now: datetime) -> str:
        st, lg = self.state, self.ledger
        if st is None or lg is None:
            return f"No session today ({now:%a %d %b}). Mode {self.store.mode.upper()}."
        b = lg.breakdown(self._ltp_by_key())
        lines = [
            f"{now:%H:%M} {self.store.mode.upper()}{' KILLED' if st.killed else ''}{' EXITING' if st.exiting else ''}",
            f"Net {rupees(b['net'])} (gross {rupees(b['gross'])}, charges {rupees(-b['charges'])})",
            f"Peak {rupees(st.peak)}, trough {rupees(st.trough)}",
            f"Floor {rupees(st.floor) if st.floor is not None else 'not armed'}"
            + (f" (re-entries {st.reentries})" if st.reentries else ""),
            f"Trades {st.trades_used(lg.trades_opened)}/{st.thresholds.max_trades} used"
            + (f" ({len(st.void_trades)} squared off on sight)" if st.void_trades else ""),
        ]
        blocked = st.blocked_reason(now, self.s.no_new_time)
        if blocked:
            lines.append(f"Trading blocked: {blocked}")
        if self.rows:
            lines.append("Open: " + ", ".join(f"{r['tradingsymbol']} x{r['quantity']}" for r in self.rows))
        if self.shorts:
            lines.append("Shorts (ignored): " + ", ".join(r["tradingsymbol"] for r in self.shorts))
        pauses = [n for n, on in (("auto-exit", st.pause_auto_exit), ("notifications", st.pause_notifications),
                                  ("square-off", st.pause_squareoff)) if on]
        if pauses:
            lines.append("PAUSED: " + ", ".join(pauses))
        if self.session is not None:
            age = self.clock() - self.session.last_tick_at if self.session.last_tick_at else None
            lines.append(f"Ticker {'connected' if self.session.ticker_connected else 'down'}"
                         + (f", last tick {age:.0f}s ago" if age is not None else ""))
        return "\n".join(lines)

    def _heartbeat(self, now: datetime) -> None:
        if self.session is None or not in_market_hours(now, self.s.market_open, self.s.market_close):
            return
        timeout = self.s.heartbeat_timeout_seconds
        if self.rows and self.session.last_tick_at and self.clock() - self.session.last_tick_at > timeout:
            self.notifier.alert(f"No ticks for {int(self.clock() - self.session.last_tick_at)}s with positions open. Check the connection.",
                                urgent=True, kind="heartbeat")
        if self.last_positions_ok and self.clock() - self.last_positions_ok > timeout:
            self.notifier.alert(f"Kite REST not responding for {int(self.clock() - self.last_positions_ok)}s.", urgent=True, kind="heartbeat_rest")
        st = self.state
        if st and (st.pause_auto_exit or st.pause_squareoff) and self.clock() - self.last_pause_reminder > 1800:
            self.last_pause_reminder = self.clock()
            self.notifier.alert("Reminder: a pause switch is ON. /status for details.")

    def _eod_summary(self, now: datetime) -> None:
        st, lg = self.state, self.ledger
        if st is None or lg is None or st.summary_sent or not at_or_after(now, self.s.summary_time):
            return
        st.summary_sent = True
        self.dirty = True
        b = lg.breakdown(self._ltp_by_key())
        exits = [o for o in st.orders if o.get("action") == "place"]
        self.notifier.alert(
            f"End of day {st.date} ({self.store.mode.upper()})\n"
            f"Gross {rupees(b['gross'])}, charges {rupees(-b['charges'])} (flat-rate estimate {rupees(-lg.flat_charge_estimate())}), "
            f"net {rupees(b['net'])}\n"
            f"Trades {lg.trades_opened} opened / {lg.trades_closed} closed; peak {rupees(st.peak)}, trough {rupees(st.trough)}\n"
            f"Lockout: {st.lockout_reason or 'none'}; re-entries {st.reentries}; exit orders {len(exits)}; "
            f"turnover {rupees(b['turnover'])}")

    # ================================================================ telegram
    def _telegram_loop(self) -> None:
        while not self.stop.is_set():
            cmds, offset = self.tg.fetch_commands(self.store.telegram_offset, self.s.telegram_poll_timeout)
            if offset != self.store.telegram_offset:
                self.store.telegram_offset = offset
            for c in cmds:
                self.commands.put(c.text)
            if not cmds:
                self.sleep(1.0)

    def _handle_commands(self, now: datetime) -> None:
        while True:
            try:
                text = self.commands.get_nowait()
            except queue.Empty:
                return
            try:
                reply = self.handle_command(text, now)
            except Exception as exc:  # noqa: BLE001
                log.exception("Command %r failed: %s", text, exc)
                reply = f"Command failed: {exc}"
            if reply:
                self.tg.send(reply)

    def handle_command(self, text: str, now: datetime) -> str:
        name, args = parse_command(text)
        st = self.state
        if name == "status":
            return self.status_text(now)
        if name == "kill":
            if st is None:
                return "No session today."
            st.killed = True
            self.dirty = True
            return "KILLED: no orders will be placed until the service restarts. Alerts continue."
        if name in ("live", "dryrun"):
            if is_trading_day(now.date()) and not before(now, self.s.pre_open):
                return f"/{name} is only accepted before {fmt_hhmm(self.s.pre_open)}."
            self.store.mode = "live" if name == "live" else "dryrun"
            if st is not None:
                st.mode = self.store.mode
            return f"Mode is now {self.store.mode.upper()}."
        if name == "exitall":
            if st is None or self.engine is None:
                return "No session today."
            st.lockout = True
            st.lockout_reason = "manual /exitall"
            self._act(Event("MANUAL_EXIT", "Manual exit-all requested.", urgent=True, exit_all=True, reason="manual"), now)
            return "Exit-all started; lockout for the day."
        if name == "set":
            return self._handle_set(args, now)
        pause_map = {
            "pause": ("pause_auto_exit", True, "Auto-exit PAUSED: rules alert only, no exit orders."),
            "resume": ("pause_auto_exit", False, "Auto-exit resumed."),
            "stop": ("pause_notifications", False, None),
            "start": ("pause_notifications", False, "Notifications on."),
            "pause_squareoff": ("pause_squareoff", True, "Lockout square-off PAUSED: new positions during lockout are alerted, not sold."),
            "resume_squareoff": ("pause_squareoff", False, "Lockout square-off resumed."),
        }
        if name in pause_map:
            if st is None:
                return "No session today."
            field_, value, msg = pause_map[name]
            if name == "stop":
                st.pause_notifications = True
                self.dirty = True
                return "Notifications muted (URGENT alerts still come through). /start to unmute."
            setattr(st, field_, value)
            self.dirty = True
            st.notes.append(f"{now:%H:%M:%S} /{name}")
            return msg
        if name in ("help", "start_help"):
            return ("/status /kill /live /dryrun /exitall /set [name value [force]] "
                    "/pause /resume /stop /start /pause_squareoff /resume_squareoff")
        return f"Unknown command: {text[:30]}"

    def _handle_set(self, args: list[str], now: datetime) -> str:
        st = self.state
        if st is None:
            return "No session today."
        th = st.thresholds
        if not args:
            return "Settable (today only):\n" + "\n".join(f"{n} = {getattr(th, n)}" for n in Thresholds.SETTABLE)
        if len(args) < 2:
            return "Usage: /set <name> <value> [force]"
        name, raw = args[0].lower(), args[1]
        force = len(args) > 2 and args[2].lower() == "force"
        if name not in Thresholds.SETTABLE:
            return f"Unknown setting {name}. /set alone lists them."
        try:
            new = int(raw) if name in ("max_trades", "cooldown_minutes") else float(raw)
        except ValueError:
            return f"Bad value {raw!r}."
        if new < 0:
            return "Values are magnitudes; use a positive number (loss_limit 200000 means -2L)."
        old = getattr(th, name)
        change = classify_change(name, old, new)
        if change == "same":
            return f"{name} is already {old}."
        market = in_market_hours(now, self.s.market_open, self.s.market_close)
        if change == "loosen" and market and not force:
            return (f"{name} {old} -> {new} LOOSENS the rule during market hours. "
                    f"Refused. Add 'force' if you mean it: /set {name} {raw} force")
        setattr(th, name, new)
        if name == "lock_floor" and st.lock_armed and st.floor is not None:
            st.floor = max(st.floor, float(new)) if change == "tighten" else float(new)
        st.settings_changes.append({"at": now.strftime("%H:%M:%S"), "name": name, "old": old, "new": new,
                                    "change": change, "forced": force})
        self.dirty = True
        note = f"/set {name} {old} -> {new} ({change}{', FORCED' if force else ''})"
        self.notifier.alert(note, urgent=force)
        return note + ". Lasts for today."

    # ================================================================ persistence
    def _persist(self) -> None:
        if self.state is None or not self.dirty or self.clock() - self.last_save < 1.0:
            return
        try:
            save_day_record(self.s.state_dir, self.state.to_dict())
            self.last_save = self.clock()
            self.dirty = False
        except Exception as exc:  # noqa: BLE001
            log.error("Could not save day record: %s", exc)


# ==================================================================== wiring

def build_live_service(settings: Settings) -> Service:
    from .kite_io import KiteSession
    store = Store(settings.state_dir)
    telegram = Telegram(settings.telegram_bot_token, settings.telegram_chat_id, settings.telegram_user_id, settings.ntfy_topic)
    holder: dict = {}

    def on_ticks(ticks):
        holder["svc"].on_ticks(ticks)

    def on_order_update(data):
        holder["svc"].on_order_update(data)

    session = KiteSession(settings.kite_api_key, settings.access_token_path, on_ticks, on_order_update)
    svc = Service(settings, store, telegram, KiteBroker(session.kite), session=session)
    holder["svc"] = svc
    return svc


def setup_logging(log_file) -> None:
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers = [logging.StreamHandler()]
    try:
        handlers.append(logging.FileHandler(log_file))
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)
    logging.getLogger("kiteconnect").setLevel(logging.WARNING)
