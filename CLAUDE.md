# Trading Assistant — context for Claude

Phase 1 "Account Guardian" for Colin's Zerodha account: watches net day P&L on bought
NIFTY/SENSEX options and places real exit orders. Replaces `pnl-monitor` (and
`long-option-live`) on the droplet.

- **Spec and all decisions:** https://claude.ai/artifact/6LngquB8RRehHFjzsQX8Hb (read it first).
- **Run:** `venv\Scripts\python -m ta`. **Tests:** `venv\Scripts\python -m pytest -q` (all must pass).
- **Deploy:** `deploy.md`. Droplet 157.245.102.152 (Ubuntu 24.04, user `pnlmon`, `/opt/<service>`),
  reachable as `root@157.245.102.152` with the ED25519 key. Its address is the Kite-whitelisted
  static IP; API orders from anywhere else are rejected.
- **Token file:** `/opt/pnl-monitor/.access_token`, refreshed 08:45 IST by `auto_token.py` (cron).
- **Old code for reference:** github.com/colindcruz/zerodha-pnl-monitor (`with-websockets/pnl_monitor.py`
  is the retired live monitor; its exit path ran for real on 2026-09-16) and the quick-exit
  dashboard (`trading-dashboard/exit_logic.py`, copied into `ta/exit_logic.py`).
- **Data for replays:** Console tradebook export (Google Drive: `colin drive/investments/`), and
  1-minute option candles on the droplet at `/opt/pnl-monitor/option_data_archive/{NIFTY,SENSEX}/<date>.json`
  (archived nightly; not recoverable if a night is missed). `replay/replay_october.py` runs the
  account rules over a tradebook; `replay/trade_rules_sim.py` runs per-trade stop/trail ladders.

## Conventions

- `ta/rules.py` and `ta/ledger.py` are pure: no I/O, fully unit-tested. Keep them that way.
- Every order goes through `ta/broker.py`; `DryRunBroker` simulates fills. Never a raw MARKET order
  (Kite rejects it); LIMIT sells below the bid inside the LPP band, prices rounded to tick.
- One exit sweep at a time; never sell what the TA is already selling (2026-09-09 near-miss).
- Thresholds are percentages of the day's capital (`opening_balance`), fixed at the open; `/set`
  can change them for the day, loosening needs `force` during market hours.
- Positions squared off on sight (lockout, cool-down, over the cap) are void trades and do not
  count toward the 4-trade cap.

## Status

See the "Implementation status" and "October replay result" sections of the spec. Open: per-trade
stop/trail (rulebook v1), weekly brake, `/book`, deploy in dry-run, 10 dry-run days, `/live`.
