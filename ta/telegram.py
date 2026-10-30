"""
Telegram: alerts out, commands in. Plus the pure command parsing and the
tighten-vs-loosen classification behind the force guard.

Only messages from TELEGRAM_USER_ID are accepted as commands (the old monitor only
checked the chat id). The getUpdates offset is persisted by the caller so a restart
never replays old commands, which the old monitor did (offset started at 0).

Send shape copied from zerodha-pnl-monitor/with-websockets/pnl_monitor.py (send_telegram,
send_ntfy); the alert-storm guard from long_option_live.py (notify_with_cooldown).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from urllib.parse import quote

import requests

log = logging.getLogger("ta.telegram")


@dataclass(frozen=True)
class Command:
    update_id: int
    text: str
    from_id: str
    chat_id: str


class Telegram:
    def __init__(self, bot_token: str, chat_id: str, user_id: str, ntfy_topic: str = "",
                 http=requests, timeout: float = 10.0):
        self.base = f"https://api.telegram.org/bot{bot_token}"
        self.chat_id = str(chat_id)
        self.user_id = str(user_id)
        self.ntfy_topic = ntfy_topic
        self.http = http
        self.timeout = timeout

    # ---- out
    def send(self, text: str) -> bool:
        try:
            resp = self.http.post(f"{self.base}/sendMessage",
                                  data={"chat_id": self.chat_id, "text": text}, timeout=self.timeout)
            if not resp.ok:
                log.error("Telegram send failed: %s %s", resp.status_code, resp.text[:200])
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("Telegram send error: %s", exc)
            return False

    def ntfy(self, title: str, body: str, priority: str = "default") -> None:
        if not self.ntfy_topic:
            return
        try:
            self.http.post(f"https://ntfy.sh/{self.ntfy_topic}", data=body.encode("utf-8"),
                           headers={"Title": quote(title), "Priority": priority}, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            log.error("ntfy send error: %s", exc)

    # ---- in
    def fetch_commands(self, offset: int, timeout: int = 20) -> tuple[list[Command], int]:
        """Long-poll getUpdates. Returns (commands from the authorised user, next offset).
        Messages from anyone else are dropped and logged. Never raises."""
        try:
            resp = self.http.get(f"{self.base}/getUpdates",
                                 params={"offset": offset, "timeout": timeout, "allowed_updates": '["message"]'},
                                 timeout=timeout + 10)
            if not resp.ok:
                log.error("getUpdates failed: %s", resp.status_code)
                return [], offset
            updates = resp.json().get("result", [])
        except Exception as exc:  # noqa: BLE001
            log.error("getUpdates error: %s", exc)
            return [], offset
        out, next_offset = [], offset
        for u in updates:
            next_offset = max(next_offset, int(u.get("update_id", 0)) + 1)
            msg = u.get("message") or {}
            text = (msg.get("text") or "").strip()
            from_id = str((msg.get("from") or {}).get("id", ""))
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            if not text:
                continue
            if from_id != self.user_id or chat_id != self.chat_id:
                log.warning("Ignored command from user %s in chat %s: %r", from_id, chat_id, text[:40])
                continue
            out.append(Command(int(u.get("update_id", 0)), text, from_id, chat_id))
        return out, next_offset


class Notifier:
    """Alerts with mute and a per-kind repeat guard. URGENT alerts always go out."""

    def __init__(self, telegram: Telegram, is_muted, clock=time.time, repeat_seconds: float = 60.0):
        self.tg = telegram
        self.is_muted = is_muted
        self.clock = clock
        self.repeat_seconds = repeat_seconds
        self._last: dict[str, tuple[float, str]] = {}

    def alert(self, text: str, urgent: bool = False, kind: str | None = None) -> None:
        if kind:
            last = self._last.get(kind)
            now = self.clock()
            if last and last[1] == text and now - last[0] < self.repeat_seconds:
                return
            self._last[kind] = (now, text)
        if self.is_muted() and not urgent:
            log.info("Muted alert: %s", text)
            return
        prefix = "\U0001f6a8 URGENT\n" if urgent else ""
        self.tg.send(prefix + text)
        if urgent:
            self.tg.ntfy("Trading Assistant", text, "urgent")


# ---------------------------------------------------------------- pure command logic

def parse_command(text: str) -> tuple[str, list[str]]:
    """'/set loss_limit 150000 force' -> ('set', ['loss_limit', '150000', 'force']).
    Lower-cases the command name only; arguments keep their case. A '@botname' suffix is dropped."""
    parts = text.strip().split()
    if not parts or not parts[0].startswith("/"):
        return "", []
    name = parts[0][1:].split("@", 1)[0].lower()
    return name, parts[1:]


# For each settable threshold: the direction of change that LOOSENS the rule.
# Loss figures are magnitudes (loss_limit 200000 means -2L).
LOOSEN_WHEN = {
    "loss_limit": "up",          # a bigger loss allowed
    "loss_warn1": "up",          # warned later
    "loss_warn2": "up",
    "lock_arm": "up",            # lock arms later
    "lock_floor": "down",        # less profit protected
    "trail_start": "up",         # trailing starts later
    "trail_distance": "up",      # more give-back allowed
    "max_trades": "up",          # more trades
    "cooldown_minutes": "down",  # shorter pause before re-entry
    "reentry_floor_step": "up",  # more locked profit risked per re-entry
}


def classify_change(name: str, old: float, new: float) -> str:
    """'tighten', 'loosen' or 'same'. Unknown names count as loosen (be safe)."""
    if new == old:
        return "same"
    direction = LOOSEN_WHEN.get(name)
    if direction is None:
        return "loosen"
    went_up = new > old
    return "loosen" if (went_up and direction == "up") or (not went_up and direction == "down") else "tighten"
