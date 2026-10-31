"""
Weekly brake and the step-up check. Pure: the service passes in the saved day records.

Spec "Weekly brake" (decisions 2026-10-31):
  * A max-loss day is a day on which the daily loss limit fired. The week is the calendar
    week (Monday to Friday); a second max-loss day on a Friday does not carry into Monday.
  * From the next trading day after the second max-loss day, every money threshold is
    halved for the rest of the week (and size should be half: 1,300 NIFTY / 500 SENSEX).
  * A third max-loss day ends the week: no trading until Monday.
  * Max-loss days are recounted at every morning setup from the saved day records. A
    missing record for a past trading day of the week (the TA was down all day) counts
    as a max-loss day, from the TA's first day onwards.
  * Step-up to 3,250 / 1,500: after 4 complete weeks each net positive, with no brake and
    no `/set ... force`. The TA reports it; nothing changes size automatically.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from .clock import is_trading_day
from .config import Thresholds

HALVED = ("loss_limit", "loss_warn1", "loss_warn2", "lock_arm", "lock_floor", "trail_start",
          "trail_distance", "reentry_floor_step")
OFF, HALF, WEEK_OVER = "off", "half", "week_over"


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def past_trading_days_this_week(today: date) -> list[date]:
    d, out = monday(today), []
    while d < today:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def count_max_loss_days(today: date, records: dict[date, dict | None], first_day: date | None) -> tuple[int, list[str]]:
    """Max-loss days so far this week (before today), and which of them were missing records."""
    count, missing = 0, []
    for d in past_trading_days_this_week(today):
        r = records.get(d)
        if r is None:
            if first_day is not None and d >= first_day:
                count += 1
                missing.append(d.isoformat())
            continue
        if r.get("loss_limit_hit"):
            count += 1
    return count, missing


def level(max_loss_days: int) -> str:
    return WEEK_OVER if max_loss_days >= 3 else HALF if max_loss_days == 2 else OFF


def halve(th: Thresholds) -> Thresholds:
    return replace(th, **{k: round(getattr(th, k) / 2) for k in HALVED})


def week_summary(records: list[dict]) -> dict:
    """One week's saved day records -> net, max-loss days, whether the brake was on, overrides."""
    return {
        "net": round(sum(r.get("last_net", 0.0) for r in records)),
        "max_loss_days": sum(1 for r in records if r.get("loss_limit_hit")),
        "brake": any(r.get("brake", OFF) != OFF for r in records),
        "overrides": sum(1 for r in records for c in r.get("settings_changes", []) if c.get("forced")),
        "days": len(records),
    }


def clean(week: dict) -> bool:
    return week["days"] > 0 and week["net"] > 0 and not week["brake"] and week["overrides"] == 0


def clean_streak(weeks: list[dict]) -> int:
    """Consecutive clean weeks ending with the last one in the list (oldest first)."""
    n = 0
    for w in reversed(weeks):
        if not clean(w):
            break
        n += 1
    return n
