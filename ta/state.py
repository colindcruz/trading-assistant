"""
Persistence: one JSON record per day (DayState) plus a small cross-day store
(mode, last good capital, Telegram offset). Atomic writes, tolerant reads.

Pattern from zerodha-pnl-monitor/long_option_persistence.py (tmp file + os.replace).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Any


def load_json(path: Path, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Store:
    """Cross-day settings that must survive restarts and new days."""

    def __init__(self, state_dir: Path):
        self.path = state_dir / "store.json"
        self.data = load_json(self.path, {})
        self.data.setdefault("mode", "dryrun")          # dryrun until /live
        self.data.setdefault("last_good_capital", 0.0)
        self.data.setdefault("telegram_offset", 0)
        self.data.setdefault("last_summary_date", "")
        self.data.setdefault("kill_pending", False)     # /kill sent with no day loaded: applies to the next day
        self.data.setdefault("live_rules", [])          # in dryrun: rules whose exits are real anyway ("/live lock")
        self.data.setdefault("first_day", "")           # the TA's first trading day: missing records count from here


    def save(self) -> None:
        save_json(self.path, self.data)

    @property
    def mode(self) -> str:
        return self.data["mode"]

    @mode.setter
    def mode(self, value: str) -> None:
        self.data["mode"] = value
        self.save()

    @property
    def live_rules(self) -> list[str]:
        return list(self.data["live_rules"])

    @live_rules.setter
    def live_rules(self, value: list[str]) -> None:
        self.data["live_rules"] = sorted(set(value))
        self.save()

    @property
    def first_day(self) -> str:
        return str(self.data["first_day"])

    @first_day.setter
    def first_day(self, value: str) -> None:
        self.data["first_day"] = value
        self.save()

    @property
    def kill_pending(self) -> bool:
        return bool(self.data["kill_pending"])

    @kill_pending.setter
    def kill_pending(self, value: bool) -> None:
        self.data["kill_pending"] = value
        self.save()

    @property
    def last_good_capital(self) -> float:
        return float(self.data["last_good_capital"])

    @last_good_capital.setter
    def last_good_capital(self, value: float) -> None:
        self.data["last_good_capital"] = value
        self.save()

    @property
    def telegram_offset(self) -> int:
        return int(self.data["telegram_offset"])

    @telegram_offset.setter
    def telegram_offset(self, value: int) -> None:
        self.data["telegram_offset"] = value
        self.save()


def day_record_path(state_dir: Path, d: date) -> Path:
    return state_dir / "days" / f"{d.isoformat()}.json"


def load_day_record(state_dir: Path, d: date) -> dict | None:
    data = load_json(day_record_path(state_dir, d), None)
    if isinstance(data, dict) and data.get("date") == d.isoformat():
        return data
    return None


def save_day_record(state_dir: Path, data: dict) -> None:
    save_json(day_record_path(state_dir, date.fromisoformat(data["date"])), data)
