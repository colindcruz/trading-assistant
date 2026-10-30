"""
The rules engine: a pure state machine over net day P&L, the clock and ledger events.

Nothing here touches Kite or Telegram. service.py feeds it `evaluate()` once a second
and `on_ledger_event()` for every fill, and acts on the Events it returns. All
thresholds come from DayState.thresholds, which /set may change intraday.

Spec: "Rules" section, with the 2026-10-30 decision that a profit-lock exit starts a
cool-down instead of ending the day, and the floor drops one trade's risk per re-entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta

from .config import Thresholds
from .ledger import LedgerEvent


# ---------------------------------------------------------------- events

@dataclass
class Event:
    kind: str
    message: str
    urgent: bool = False
    exit_all: bool = False          # service runs the exit-all sweep
    square_off: str | None = None   # contract key to square off (whole open quantity)
    square_off_qty: int | None = None  # or just this many (an add during lockout)
    reason: str = ""


# ---------------------------------------------------------------- day state

@dataclass
class DayState:
    date: str
    thresholds: Thresholds
    mode: str = "dryrun"
    capital_source: str = ""

    peak: float = 0.0
    trough: float = 0.0
    last_net: float = 0.0

    warn1_sent: bool = False
    warn2_sent: bool = False
    loss_limit_hit: bool = False

    lock_armed: bool = False
    floor: float | None = None
    trail_anchor_peak: float | None = None   # after a lock exit: floor only ratchets once peak beats this
    last_floor_alerted: float | None = None
    reentries: int = 0

    cooldown_until: datetime | None = None
    cooldown_announced: bool = True

    lockout: bool = False
    lockout_reason: str = ""
    eod_exit_done: bool = False
    exiting: bool = False           # a sweep is running (also persisted so a restart mid-sweep knows)
    killed: bool = False            # /kill: no order placement until restart

    pause_auto_exit: bool = False
    pause_notifications: bool = False
    pause_squareoff: bool = False

    setup_sent: bool = False
    guardian_check_sent: bool = False
    summary_sent: bool = False

    void_trades: list[int] = field(default_factory=list)   # ledger trade numbers that were squared off on sight; not counted
    orders: list[dict] = field(default_factory=list)
    settings_changes: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------ persistence
    def to_dict(self) -> dict:
        d = asdict(self)
        d["thresholds"] = self.thresholds.to_dict()
        d["cooldown_until"] = self.cooldown_until.isoformat() if self.cooldown_until else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "DayState":
        d = dict(d)
        d["thresholds"] = Thresholds.from_dict(d["thresholds"])
        cu = d.get("cooldown_until")
        d["cooldown_until"] = datetime.fromisoformat(cu) if cu else None
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    # ------------------------------------------------------------ queries
    def in_cooldown(self, now: datetime) -> bool:
        return self.cooldown_until is not None and now < self.cooldown_until

    def cooldown_seconds_left(self, now: datetime) -> int:
        if not self.in_cooldown(now):
            return 0
        return int((self.cooldown_until - now).total_seconds())

    def blocked_reason(self, now: datetime, no_new_time: tuple[int, int]) -> str | None:
        """Why a new position is not allowed right now, or None if trading is open."""
        if self.lockout:
            return f"lockout ({self.lockout_reason})"
        if self.in_cooldown(now):
            return f"cool-down ({self.cooldown_seconds_left(now)}s left)"
        if (now.hour, now.minute) >= no_new_time:
            return "after the cut-off"
        return None

    def counted_trade_no(self, ledger_trade_no: int) -> int:
        """The ledger numbers every open-from-flat as a trade; positions squared off on
        sight (during lockout, cool-down, after the cut-off, or over the cap) are void
        and do not count. This is the trade's number as the trader sees it."""
        return ledger_trade_no - sum(1 for v in self.void_trades if v <= ledger_trade_no)

    def trades_used(self, trades_opened: int) -> int:
        return self.counted_trade_no(trades_opened)

    def trades_left(self, trades_opened: int) -> int:
        return max(0, self.thresholds.max_trades - self.trades_used(trades_opened))


# ---------------------------------------------------------------- rules

def evaluate(state: DayState, net: float, now: datetime, has_open: bool,
             exit_all_time: tuple[int, int]) -> list[Event]:
    """Run the P&L and time rules for one tick. Mutates `state`; returns events in order.

    `has_open`: whether any NIFTY/SENSEX option position is open. A loss-limit hit with a
    flat book still locks the day; a floor hit with a flat book does nothing (the floor
    only matters while something can still lose).
    """
    th = state.thresholds
    events: list[Event] = []
    state.last_net = net
    state.peak = max(state.peak, net)
    state.trough = min(state.trough, net)

    if state.exiting:
        return events  # a sweep is in progress; no second trigger (spec: exit-all step 1)

    # --- loss warnings and limit (apply in every state, including cool-down and lockout)
    if not state.warn1_sent and net <= -th.loss_warn1:
        state.warn1_sent = True
        events.append(Event("WARN1", f"Loss warning 1: net {_r(net)} (limit {_r(-th.loss_limit)})"))
    if not state.warn2_sent and net <= -th.loss_warn2:
        state.warn2_sent = True
        events.append(Event("WARN2", f"Loss warning 2: net {_r(net)}. Cut size. Limit {_r(-th.loss_limit)}", urgent=True))
    if not state.loss_limit_hit and net <= -th.loss_limit:
        state.loss_limit_hit = True
        state.lockout = True
        state.lockout_reason = "loss limit"
        events.append(Event("LOSS_EXIT", f"LOSS LIMIT {_r(-th.loss_limit)} hit: net {_r(net)}. Exiting all and locking the day.",
                            urgent=True, exit_all=has_open, reason="loss limit"))
        return events

    # --- profit lock arms
    if not state.lock_armed and net >= th.lock_arm:
        state.lock_armed = True
        state.floor = float(th.lock_floor)
        state.last_floor_alerted = state.floor
        state.trail_anchor_peak = None
        events.append(Event("LOCK_ARMED", f"Profit lock armed at {_r(net)}: floor {_r(state.floor)}"))

    # --- trailing floor ratchet (never down, except the re-entry step applied at a lock exit)
    if state.lock_armed and state.floor is not None and state.peak >= th.trail_start:
        if state.trail_anchor_peak is None or state.peak > state.trail_anchor_peak:
            new_floor = max(state.floor, state.peak - th.trail_distance)
            if new_floor > state.floor:
                state.floor = new_floor
                if state.last_floor_alerted is None or state.floor - state.last_floor_alerted >= th.floor_alert_step:
                    state.last_floor_alerted = state.floor
                    events.append(Event("FLOOR_RAISED", f"Floor raised to {_r(state.floor)} (peak {_r(state.peak)})"))

    # --- profit lock exit -> cool-down, floor drops one trade's risk
    if (state.lock_armed and state.floor is not None and has_open and not state.lockout
            and not state.in_cooldown(now) and net <= state.floor):
        floor_hit = state.floor
        state.floor = floor_hit - th.reentry_floor_step
        state.trail_anchor_peak = state.peak
        state.reentries += 1
        state.cooldown_until = now + timedelta(minutes=th.cooldown_minutes)
        state.cooldown_announced = False
        events.append(Event(
            "LOCK_EXIT",
            f"PROFIT LOCK floor {_r(floor_hit)} hit: net {_r(net)}. Exiting all. "
            f"Cool-down {th.cooldown_minutes} min, then the floor is {_r(state.floor)}.",
            urgent=True, exit_all=True, reason="profit lock"))
        return events

    # --- cool-down over
    if state.cooldown_until is not None and not state.cooldown_announced and now >= state.cooldown_until:
        state.cooldown_announced = True
        events.append(Event("COOLDOWN_OVER", f"Cool-down over. Floor {_r(state.floor)}, net {_r(net)}."))

    # --- end of day
    if not state.eod_exit_done and (now.hour, now.minute) >= exit_all_time and not state.lockout:
        state.eod_exit_done = True
        state.lockout = True
        state.lockout_reason = "end of day"
        events.append(Event("EOD_EXIT", f"{now.strftime('%H:%M')}: exiting all open positions for the day.",
                            urgent=has_open, exit_all=has_open, reason="end of day"))
    elif not state.eod_exit_done and (now.hour, now.minute) >= exit_all_time:
        state.eod_exit_done = True
        if has_open:
            events.append(Event("EOD_EXIT", f"{now.strftime('%H:%M')}: exiting remaining positions.",
                                urgent=True, exit_all=True, reason="end of day"))
    return events


def on_ledger_event(state: DayState, ev: LedgerEvent, now: datetime, trades_opened: int,
                    no_new_time: tuple[int, int]) -> list[Event]:
    """React to a fill: the trade counter and the no-new-positions rules."""
    th = state.thresholds
    events: list[Event] = []
    sym = ev.fill.tradingsymbol
    blocked = state.blocked_reason(now, no_new_time)

    if ev.kind == "TRADE_OPENED":
        n = state.counted_trade_no(ev.trade_no)
        if blocked:
            state.void_trades.append(ev.trade_no)
            events.append(Event("BLOCKED_SQUAREOFF", f"New position {sym} x{ev.fill.quantity} during {blocked}: squaring off.",
                                urgent=True, square_off=ev.key, reason=blocked))
        elif n > th.max_trades:
            state.void_trades.append(ev.trade_no)
            events.append(Event("TRADE_CAP_SQUAREOFF",
                                f"Trade {n} of {th.max_trades} opened ({sym}): over the cap, squaring off.",
                                urgent=True, square_off=ev.key, reason="trade cap"))
        else:
            events.append(Event("TRADE_OPENED", f"Trade {n} of {th.max_trades} opened: {sym} x{ev.fill.quantity} @ {ev.fill.price}"))
    elif ev.kind == "POSITION_INCREASED":
        if blocked:
            events.append(Event("BLOCKED_SQUAREOFF", f"Added {sym} x{ev.fill.quantity} during {blocked}: squaring off the add.",
                                urgent=True, square_off=ev.key, square_off_qty=ev.fill.quantity, reason=blocked))
    elif ev.kind == "TRADE_CLOSED":
        if ev.trade_no in state.void_trades:
            return events   # a square-off completing; nothing to announce
        n = state.counted_trade_no(ev.trade_no)
        events.append(Event("TRADE_CLOSED", f"Trade {n} closed: {sym}"))
        if n >= th.max_trades and not state.lockout:
            state.lockout = True
            state.lockout_reason = "trade cap"
            events.append(Event("TRADE_CAP_LOCKOUT", f"Trade {th.max_trades} closed: trading is done for the day.", urgent=True))
    return events


def _r(x: float) -> str:
    from .clock import rupees
    return rupees(x)
