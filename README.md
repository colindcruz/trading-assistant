# Trading Assistant — Phase 1: Account Guardian

One service that watches net day P&L on bought NIFTY and SENSEX options in a Zerodha
account, places real exit orders when a rule trips, and stops new trading once the day is
over. It replaces `pnl-monitor` and `long-option-live` on the droplet.

Spec and decisions: https://claude.ai/artifact/6LngquB8RRehHFjzsQX8Hb

## What it enforces

| Rule | At ₹20L capital |
|---|---|
| Loss warnings | −₹1L, −₹1.5L (alerts) |
| Loss limit | −₹2L: exit all, lockout for the day |
| Profit lock | arms at +₹2L with a floor at +₹1.33L; from a +₹2.5L peak the floor trails ₹68k below the peak, never down |
| Lock exit | net at the floor: exit all, 3-minute cool-down, then re-entry allowed with the floor ₹50k lower |
| Trade cap | 4 trades; a 5th is squared off; when the 4th closes, lockout |
| Time | 15:06 exit all; no new positions after 15:10; summary after 15:30 |

Net P&L = booked + open (marked to live ticks) − charges computed per fill.
Capital is Kite's `opening_balance` plus pledged-holdings `collateral`, read before the open and fixed for the day.

## Telegram

`/status`, `/kill`, `/live`, `/dryrun` (both only before 09:15), `/exitall`,
`/set` (list) or `/set <name> <value> [force]`, `/pause` `/resume` (auto-exit),
`/stop` `/start` (notifications), `/pause_squareoff` `/resume_squareoff`.

Loosening a rule during market hours needs `force`. Pauses expire at the next session.
Only `TELEGRAM_USER_ID` is accepted.

## Run locally

```
python -m venv venv && venv\Scripts\pip install -r requirements.txt
copy .env.example .env   # fill in
venv\Scripts\python -m pytest -q
venv\Scripts\python -m ta
```

Deployment: see `deploy.md`. Replay of a Zerodha tradebook: `python replay/replay_october.py <tradebook.csv>`.

## Layout

`ta/rules.py` and `ta/ledger.py` are pure (no I/O) and carry the logic. `ta/exit_engine.py`
runs the exit sweep against a `Broker` (`ta/broker.py`: Kite, dry-run, or a test fake).
`ta/service.py` is the loop. `ta/exit_logic.py` is copied from the quick-exit dashboard.
