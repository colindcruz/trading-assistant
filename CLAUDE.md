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
  account rules over a tradebook (what-ifs: `target`, `lock_ends_day`, `--qty-scale`);
  `replay/trade_ladder_sim.py` compares per-trade ladders at rulebook size, with slippage and a
  day-level loss limit (`run_day`); `replay/trade_adds_sim.py` tests stop policies on adds.

## Conventions

- `ta/rules.py`, `ledger.py`, `stops.py`, `brake.py`, `slippage.py` and `watchlist.py` are pure:
  no I/O, fully unit-tested. Keep them that way.
- Every order goes through `ta/broker.py`; `DryRunBroker` simulates fills. Never a raw MARKET order
  (Kite rejects it); LIMIT sells below the bid inside the LPP band, prices rounded to tick.
- One exit sweep at a time; never sell what the TA is already selling (2026-09-09 near-miss).
- Thresholds are percentages of the day's capital (`opening_balance`), fixed at the open; `/set`
  can change them for the day, loosening needs `force` during market hours.
- No daily trade cap (`MAX_TRADES=0`, decided 2026-10-31); the -2L loss limit ends the day.
  Positions squared off on sight (lockout, cool-down) are void trades and are not counted.
- Modes: `/dryrun`; `/live <rule>` (e.g. `/live lock`) makes only that rule's exits real while the
  others alert without simulating; `/live` is everything. Per-trade stops sell only with
  `STOP_EXITS=live` AND `/live`; otherwise they alert and record (v1 and wide ladders side by side).
- Deploy = `git archive HEAD` to the droplet, `pytest` there, `systemctl restart trading-assistant`;
  `/opt/trading-assistant/DEPLOYED_COMMIT` records what runs. Mode and live rules live in `store.json`.

## Status

The spec's last section, "Open items", is the to-do list; keep it current. As of 2026-10-31:
deployed in dry-run (`baf037c`) with the stop/trail engine (log mode), weekly brake, slippage
logging and near-the-money watchlist. First session Monday 2026-11-02; `/live lock` planned from
Tuesday if Monday's checks pass. Code is frozen while Colin is away (2026-11-02 to about
2026-11-14). Next: rerun the replays on November's day files and candles, decide v1 vs wide,
check real slippage, then build `/book` and the pinned live summary.
