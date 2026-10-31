"""
Settings for the Trading Assistant, read once from the environment (.env).

Percentages here are of the day's capital C (read from Kite before the open, see
service.py). `Thresholds.from_capital` turns them into rupee figures that stay fixed
for the day. Everything that /set can change intraday lives on Thresholds; the
Settings object itself is immutable after startup.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def _env_float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _env_int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _env_hhmm(name: str, default: str) -> tuple[int, int]:
    h, m = _env(name, default).split(":")
    return int(h), int(m)


@dataclass(frozen=True)
class ChargeRates:
    """Zerodha charges for index options, per zerodha.com/charges on 2026-10-30.
    STT rose from 0.10% to 0.15% on 2026-04-01 (Budget 2026-27)."""
    stt_sell_pct: float = 0.15          # of sell premium value
    exch_nfo_pct: float = 0.03553       # NSE transaction charge, of premium, both sides
    exch_bfo_pct: float = 0.0325        # BSE transaction charge
    gst_pct: float = 18.0               # on brokerage + exchange + SEBI charges
    brokerage_per_order: float = 20.0   # per executed order
    stamp_buy_pct: float = 0.003        # of buy value
    sebi_per_crore: float = 10.0        # of turnover
    flat_rate_pct: float = 0.117        # fallback: October 2026 statement, Rs 11,700 per crore

    @classmethod
    def from_env(cls) -> "ChargeRates":
        return cls(
            stt_sell_pct=_env_float("CHARGE_STT_SELL_PCT", cls.stt_sell_pct),
            exch_nfo_pct=_env_float("CHARGE_EXCH_NFO_PCT", cls.exch_nfo_pct),
            exch_bfo_pct=_env_float("CHARGE_EXCH_BFO_PCT", cls.exch_bfo_pct),
            gst_pct=_env_float("CHARGE_GST_PCT", cls.gst_pct),
            brokerage_per_order=_env_float("CHARGE_BROKERAGE_PER_ORDER", cls.brokerage_per_order),
            stamp_buy_pct=_env_float("CHARGE_STAMP_BUY_PCT", cls.stamp_buy_pct),
            sebi_per_crore=_env_float("CHARGE_SEBI_PER_CRORE", cls.sebi_per_crore),
            flat_rate_pct=_env_float("CHARGE_FLAT_RATE_PCT", cls.flat_rate_pct),
        )


@dataclass(frozen=True)
class RulePercents:
    """Risk parameters as percentages of capital, plus the counts and minutes that
    are not percentages. Spec 'Risk parameters' table."""
    loss_limit_pct: float = 10.0
    loss_warn1_pct: float = 5.0
    loss_warn2_pct: float = 7.5
    lock_arm_pct: float = 12.5
    lock_floor_pct: float = 8.33
    trail_start_pct: float = 15.6
    trail_distance_pct: float = 4.25
    max_trades: int = 0                        # 0 = no daily trade cap (decided 2026-10-31)
    cooldown_minutes: int = 3
    risk_per_trade_pct: float = 2.5            # Rs 50k at Rs 20L
    reentry_floor_step: float | None = None    # None -> risk per trade
    floor_alert_step: float = 25_000.0

    @classmethod
    def from_env(cls) -> "RulePercents":
        step = _env("REENTRY_FLOOR_STEP", "")
        return cls(
            loss_limit_pct=_env_float("LOSS_LIMIT_PCT", cls.loss_limit_pct),
            loss_warn1_pct=_env_float("LOSS_WARN1_PCT", cls.loss_warn1_pct),
            loss_warn2_pct=_env_float("LOSS_WARN2_PCT", cls.loss_warn2_pct),
            lock_arm_pct=_env_float("LOCK_ARM_PCT", cls.lock_arm_pct),
            lock_floor_pct=_env_float("LOCK_FLOOR_PCT", cls.lock_floor_pct),
            trail_start_pct=_env_float("TRAIL_START_PCT", cls.trail_start_pct),
            trail_distance_pct=_env_float("TRAIL_DISTANCE_PCT", cls.trail_distance_pct),
            max_trades=_env_int("MAX_TRADES", cls.max_trades),
            cooldown_minutes=_env_int("COOLDOWN_MINUTES", cls.cooldown_minutes),
            risk_per_trade_pct=_env_float("RISK_PER_TRADE_PCT", cls.risk_per_trade_pct),
            reentry_floor_step=float(step) if step else None,
            floor_alert_step=_env_float("FLOOR_ALERT_STEP", cls.floor_alert_step),
        )


@dataclass
class Thresholds:
    """Rupee thresholds for one day. Loss figures are positive magnitudes: the loss
    limit fires when net P&L <= -loss_limit. These are the values /set changes."""
    capital: float
    loss_limit: float
    loss_warn1: float
    loss_warn2: float
    lock_arm: float
    lock_floor: float
    trail_start: float
    trail_distance: float
    max_trades: int
    cooldown_minutes: int
    reentry_floor_step: float
    floor_alert_step: float

    # Names /set accepts, and which direction counts as loosening (see telegram.classify_change)
    SETTABLE = ("loss_limit", "loss_warn1", "loss_warn2", "lock_arm", "lock_floor", "trail_start",
                "trail_distance", "max_trades", "cooldown_minutes", "reentry_floor_step")

    @classmethod
    def from_capital(cls, capital: float, p: RulePercents) -> "Thresholds":
        loss_limit = round(capital * p.loss_limit_pct / 100)
        return cls(
            capital=capital,
            loss_limit=loss_limit,
            loss_warn1=round(capital * p.loss_warn1_pct / 100),
            loss_warn2=round(capital * p.loss_warn2_pct / 100),
            lock_arm=round(capital * p.lock_arm_pct / 100),
            lock_floor=round(capital * p.lock_floor_pct / 100),
            trail_start=round(capital * p.trail_start_pct / 100),
            trail_distance=round(capital * p.trail_distance_pct / 100),
            max_trades=p.max_trades,
            cooldown_minutes=p.cooldown_minutes,
            reentry_floor_step=p.reentry_floor_step if p.reentry_floor_step is not None
            else round(capital * p.risk_per_trade_pct / 100),
            floor_alert_step=p.floor_alert_step,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Thresholds":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass(frozen=True)
class Settings:
    # --- secrets / identity ---
    kite_api_key: str
    access_token_path: Path
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_user_id: str
    ntfy_topic: str

    # --- rules ---
    percents: RulePercents
    charges: ChargeRates
    capital_sanity_pct: float          # morning capital read must be within this % of last good
    capital_override: float            # >0 forces capital (testing / Kite outage), else read from margins

    # --- exits ---
    exit_buffer_pct: float             # limit price = bid * (1 - buffer)
    reprice_attempts: int
    reprice_interval_seconds: float
    urgent_after_seconds: float
    order_gap_seconds: float           # pacing between orders (5/s)
    market_protection: int             # for the MARKET fallback only
    skip_below_price: float            # 0 = never skip; else positions with LTP below this are left alone (alert says so)
    freeze_qty: dict[str, int]
    cancel_wait_seconds: float

    # --- times (IST) ---
    pre_open: tuple[int, int]
    market_open: tuple[int, int]
    exit_all_time: tuple[int, int]
    no_new_time: tuple[int, int]
    summary_time: tuple[int, int]
    market_close: tuple[int, int]
    guardian_check_time: tuple[int, int]

    # --- infra ---
    state_dir: Path
    log_file: Path
    heartbeat_timeout_seconds: float
    positions_poll_seconds: float
    token_check_seconds: float
    telegram_poll_timeout: int

    # --- near-the-money watchlist (slippage references for entries; see watchlist.py) ---
    watch_strikes: int = 10            # strikes each side of the money, CE and PE; 0 = off
    watch_expiries: int = 2            # nearest expiries per index
    watch_refresh_seconds: float = 60.0

    @classmethod
    def from_env(cls) -> "Settings":
        state_dir = Path(_env("STATE_DIR", "."))
        return cls(
            kite_api_key=_env("KITE_API_KEY", ""),
            access_token_path=Path(_env("ACCESS_TOKEN_PATH", ".access_token")),
            telegram_bot_token=_env("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=_env("TELEGRAM_CHAT_ID", ""),
            telegram_user_id=_env("TELEGRAM_USER_ID", ""),
            ntfy_topic=_env("NTFY_TOPIC", ""),
            percents=RulePercents.from_env(),
            charges=ChargeRates.from_env(),
            capital_sanity_pct=_env_float("CAPITAL_SANITY_PCT", 50.0),
            capital_override=_env_float("CAPITAL_OVERRIDE", 0.0),
            exit_buffer_pct=_env_float("EXIT_BUFFER_PCT", 5.0),
            reprice_attempts=_env_int("REPRICE_ATTEMPTS", 3),
            reprice_interval_seconds=_env_float("REPRICE_INTERVAL_SECONDS", 2.0),
            urgent_after_seconds=_env_float("URGENT_AFTER_SECONDS", 30.0),
            order_gap_seconds=_env_float("ORDER_GAP_SECONDS", 0.2),
            market_protection=_env_int("MARKET_PROTECTION", -1),
            skip_below_price=_env_float("SKIP_BELOW_PRICE", 0.0),
            freeze_qty={
                # NIFTY 3,510 (54 lots of 65): NSE circular FAOP/76693, effective 2026-10-05.
                # SENSEX 1,000 (50 lots of 20): BSE single-order limit since 2023-12-04,
                # confirmed from broker tables 2026-10-30. NSE revises these several times a year.
                "NIFTY": _env_int("FREEZE_QTY_NIFTY", 3510),
                "SENSEX": _env_int("FREEZE_QTY_SENSEX", 1000),
            },
            cancel_wait_seconds=_env_float("CANCEL_WAIT_SECONDS", 3.0),
            pre_open=_env_hhmm("PRE_OPEN_TIME", "09:15"),
            market_open=_env_hhmm("MARKET_OPEN_TIME", "09:15"),
            exit_all_time=_env_hhmm("EXIT_ALL_TIME", "15:06"),
            no_new_time=_env_hhmm("NO_NEW_TIME", "15:10"),
            summary_time=_env_hhmm("SUMMARY_TIME", "15:30"),
            market_close=_env_hhmm("MARKET_CLOSE_TIME", "15:40"),
            guardian_check_time=_env_hhmm("GUARDIAN_CHECK_TIME", "09:00"),
            state_dir=state_dir,
            log_file=Path(_env("LOG_FILE", str(state_dir / "trading_assistant.log"))),
            heartbeat_timeout_seconds=_env_float("HEARTBEAT_TIMEOUT_SECONDS", 30.0),
            positions_poll_seconds=_env_float("POSITIONS_POLL_SECONDS", 2.0),
            token_check_seconds=_env_float("TOKEN_CHECK_SECONDS", 30.0),
            telegram_poll_timeout=_env_int("TELEGRAM_POLL_TIMEOUT", 20),
            watch_strikes=_env_int("WATCH_STRIKES", 10),
            watch_expiries=_env_int("WATCH_EXPIRIES", 2),
            watch_refresh_seconds=_env_float("WATCH_REFRESH_SECONDS", 60.0),
        )

    def validate(self) -> list[str]:
        """Problems that must stop startup. Empty list means OK."""
        problems = []
        if not self.kite_api_key:
            problems.append("KITE_API_KEY is not set")
        if not self.telegram_bot_token or not self.telegram_chat_id:
            problems.append("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set")
        if not self.telegram_user_id:
            problems.append("TELEGRAM_USER_ID is not set (commands are only accepted from this user)")
        if not 0 < self.exit_buffer_pct < 40:
            problems.append("EXIT_BUFFER_PCT must be between 0 and 40 (the LPP band is +/-40%)")
        if self.percents.max_trades < 0:
            problems.append("MAX_TRADES must be 0 (no cap) or more")
        if self.percents.lock_floor_pct >= self.percents.lock_arm_pct:
            problems.append("LOCK_FLOOR_PCT must be below LOCK_ARM_PCT")
        return problems
