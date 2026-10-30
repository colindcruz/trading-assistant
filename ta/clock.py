"""
IST time helpers and the trading calendar. Pure: every function takes `now` so
tests can pin the clock.

Holiday list copied from zerodha-pnl-monitor/with-websockets/pnl_monitor.py
(verified there on 2026-08-15 against two sources). Needs a manual update every
year; nothing fetches it automatically. The 2026-11-08 Sunday Muhurat session is
deliberately not a trading day here.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

NSE_HOLIDAYS = {
    "2026-01-15",  # Maharashtra municipal elections
    "2026-01-26",  # Republic Day
    "2026-03-03",  # Holi
    "2026-03-26",  # Shri Ram Navami
    "2026-03-31",  # Shri Mahavir Jayanti
    "2026-04-03",  # Good Friday
    "2026-04-14",  # Dr. Baba Saheb Ambedkar Jayanti
    "2026-05-01",  # Maharashtra Day
    "2026-05-28",  # Bakri Id
    "2026-06-26",  # Muharram
    "2026-09-14",  # Ganesh Chaturthi
    "2026-10-02",  # Mahatma Gandhi Jayanti
    "2026-10-20",  # Dussehra
    "2026-11-10",  # Diwali - Balipratipada
    "2026-11-24",  # Prakash Gurpurb Sri Guru Nanak Dev
    "2026-12-25",  # Christmas
}


def now_ist() -> datetime:
    return datetime.now(IST)


def is_holiday(d: date) -> bool:
    return d.isoformat() in NSE_HOLIDAYS


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and not is_holiday(d)


def next_trading_day(d: date) -> date:
    d = d + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def hm(now: datetime) -> tuple[int, int]:
    return now.hour, now.minute


def at_or_after(now: datetime, t: tuple[int, int]) -> bool:
    return hm(now) >= t


def before(now: datetime, t: tuple[int, int]) -> bool:
    return hm(now) < t


def fmt_hhmm(t: tuple[int, int]) -> str:
    """(15, 10) -> '3:10pm'."""
    hour, minute = t
    suffix = "am" if hour < 12 else "pm"
    display = hour if hour <= 12 else hour - 12
    return f"{display}:{minute:02d}{suffix}"


def in_market_hours(now: datetime, open_t: tuple[int, int], close_t: tuple[int, int]) -> bool:
    return is_trading_day(now.date()) and open_t <= hm(now) <= close_t


def rupees(x: float) -> str:
    """Compact Indian-style formatting for alerts: 1.33L, 68k, -2.00L."""
    sign = "-" if x < 0 else "+" if x > 0 else ""
    a = abs(x)
    if a >= 1_00_000:
        return f"{sign}₹{a / 1_00_000:.2f}L"
    if a >= 1_000:
        return f"{sign}₹{a / 1_000:.0f}k"
    return f"{sign}₹{a:.0f}"
