# Deploying the Trading Assistant on the droplet

Target: the existing DigitalOcean droplet (157.245.102.152), Ubuntu 24.04, service user
`pnlmon`, the same layout as `/opt/pnl-monitor`. The droplet's address is the static IP
whitelisted on the Kite Connect developer profile; order requests from anywhere else are
rejected (SEBI rule, in force since 2026-04-01).

## 1. Install

```
ssh root@157.245.102.152
mkdir -p /opt/trading-assistant && cd /opt/trading-assistant
# copy ta/, requirements.txt, trading-assistant.service, .env.example from this repo (scp or git)
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env && nano .env     # fill in keys, TELEGRAM_USER_ID
chown -R pnlmon:pnlmon /opt/trading-assistant
```

`.env` must set:
- `KITE_API_KEY` and `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`: the same values as `/opt/pnl-monitor/.env`.
- `TELEGRAM_USER_ID`: your own Telegram user id (message @userinfobot).
- `ACCESS_TOKEN_PATH=/opt/pnl-monitor/.access_token` (written by `auto_token.py` every weekday 08:45).

The service starts in DRY-RUN. It stays there until `/live` is sent before 09:15 on some
morning; the mode persists across days in `store.json`.

## 2. Retire the services it replaces

```
systemctl disable --now pnl-monitor long-option-live
```

Keep their unit files and directories. `pnl-monitor`'s `level_state.json` still says
`day_locked` from 16 Sep 2026; harmless while disabled. `quick-exit-dashboard` keeps
running (it reads the token file every 30 s and uses one of the three websocket slots;
the TA uses a second).

## 3. Token refresh

`/opt/pnl-monitor/auto_token.py` runs `systemctl try-restart pnl-monitor` with `check=True`.
With pnl-monitor stopped that is a no-op and still succeeds, so the cron keeps working.
The TA does not need a restart: it re-reads the token file every 30 s and swaps the
session and ticker in place. Optionally add `trading-assistant` to the try-restart list
anyway (`check=False`).

## 4. Start

```
cp trading-assistant.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now trading-assistant
journalctl -u trading-assistant -n 40 -f
```

Expect: "Instruments loaded", "Ticker connected", and at 09:00 a Telegram message saying
the Trading Assistant is running in DRY-RUN, followed by the morning setup with capital
and thresholds.

## 5. First morning checks

1. `/status` answers.
2. `/set` lists the day's thresholds.
3. Watch one dry-run day: every would-be order appears in the log and in `days/<date>.json`.
4. After the 15:06 exit, `/kill` answers "KILLED". A kill lasts the rest of the day, restarts included
   (it is saved in the day record), so testing it in the morning would lose that day's dry-run.
   A `/kill` sent when no day is loaded (weekend, before the session) applies to the next trading day.

## 6. Going live

After ten clean dry-run days (compare each day's `days/<date>.json` orders with the Kite
tradebook): send `/live` before 09:15, then `/status` to confirm LIVE. Test `/kill` after the
close the day before, not that morning (it would stop the first live day).

## 7. Backups

`backup-env.sh` in zerodha-pnl-monitor archives every `/opt/*/.env`, so the TA's `.env`
is included automatically. `days/` and `store.json` are regenerated; no backup needed.
