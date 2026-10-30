"""
The exit-all sweep. Spec "Order execution and safety":

  1. Mark the day as exiting, so no rule can trigger a second sweep.
  2. Cancel all open and pending orders for NIFTY and SENSEX options, including stop-losses.
  3. For each open position, one SELL per freeze-limit slice, LIMIT a buffer below the
     best bid (market orders on options may be restricted).
  4. Every 2 s re-read positions; re-price unfilled orders closer to the bid, up to 3 times.
  5. Still open after 30 s: URGENT alert to exit manually.
  6. All flat: report.

Modelled on trading-dashboard/exit_service.py (cancel-then-sell, pacing, tags, order
tracking) and zerodha-pnl-monitor's cool-off guard (one sweep at a time). What it will
never do: BUY (shorts are out of scope and are only reported), send a second SELL for a
position that already has one of this sweep's orders live, or send a raw MARKET order.

On 2026-09-09 the old monitor's two enforcement paths sold the same put twice, two
seconds apart, and the second order (against an already-flat book) would have opened a
naked short had margin allowed it. That is why every sweep re-reads positions right
before placing, caps each SELL at (open quantity - our own resting SELLs), and why only
one sweep can run at a time.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from .config import Settings
from .exit_logic import (LONG, SHORT, TERMINAL_STATUSES, is_open_order, lpp_floor, pending_sell_qty,
                         round_to_tick, same_contract, select_index_options, split_for_freeze)

log = logging.getLogger("ta.exit")

TAG_PREFIX = "TA"


@dataclass
class SweepResult:
    reason: str
    started_at: str = ""
    already_running: bool = False
    nothing_to_do: bool = False
    rows: list[dict] = field(default_factory=list)
    shorts: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    cancelled: int = 0
    placed: list[dict] = field(default_factory=list)     # one per order sent (final status filled in)
    failed: list[str] = field(default_factory=list)
    still_open: list[str] = field(default_factory=list)
    elapsed: float = 0.0

    @property
    def flat(self) -> bool:
        return not self.still_open

    def summary(self) -> str:
        if self.already_running:
            return "exit already in progress"
        if self.nothing_to_do:
            return "nothing to exit" + (f" (skipped: {', '.join(self.skipped)})" if self.skipped else "")
        parts = [f"{len(self.placed)} order(s) for {len(self.rows)} position(s), {self.cancelled} cancelled"]
        filled = sum(int(o.get("filled", 0)) for o in self.placed)
        parts.append(f"filled {filled}")
        if self.failed:
            parts.append("FAILED: " + "; ".join(self.failed))
        if self.skipped:
            parts.append("skipped: " + ", ".join(self.skipped))
        if self.shorts:
            parts.append("shorts left alone: " + ", ".join(self.shorts))
        parts.append("FLAT" if self.flat else "STILL OPEN: " + ", ".join(self.still_open))
        return "; ".join(parts) + f" ({self.elapsed:.0f}s)"


class ExitEngine:
    def __init__(self, broker, settings: Settings, instruments: dict, notify, record_order,
                 clock=time.time, sleep=time.sleep, now_fn=None):
        self.broker = broker
        self.s = settings
        self.instruments = instruments
        self.notify = notify                  # notify(text, urgent=False)
        self.record_order = record_order      # record_order(dict) -> persisted in the day record
        self.clock = clock
        self.sleep = sleep
        self.now_fn = now_fn or datetime.now
        self._sweep_lock = threading.Lock()
        self._pace_lock = threading.Lock()
        self._last_order_at = 0.0
        self._sweep_no = 0
        self._shorts_reported: set[str] = set()

    # ------------------------------------------------------------ reads
    def long_rows(self, net: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
        """(long NIFTY/SENSEX option rows, short rows)."""
        rows, _unknown = select_index_options(net if net is not None else self.broker.positions_net(), self.instruments)
        return [r for r in rows if r["side"] == LONG], [r for r in rows if r["side"] == SHORT]

    # ------------------------------------------------------------ public entry points
    def exit_all(self, reason: str) -> SweepResult:
        return self._sweep(reason, keys=None, qty_cap=None)

    def square_off(self, key: str, reason: str, qty: int | None = None) -> SweepResult:
        return self._sweep(reason, keys={key}, qty_cap=qty)

    # ------------------------------------------------------------ the sweep
    def _sweep(self, reason: str, keys: set[str] | None, qty_cap: int | None) -> SweepResult:
        result = SweepResult(reason=reason)
        if not self._sweep_lock.acquire(blocking=False):
            result.already_running = True
            log.warning("Exit (%s) requested while another sweep is running; ignored", reason)
            return result
        try:
            return self._run(result, keys, qty_cap)
        finally:
            self._sweep_lock.release()

    def _run(self, result: SweepResult, keys: set[str] | None, qty_cap: int | None) -> SweepResult:
        t0 = self.clock()
        self._sweep_no += 1
        now = self.now_fn()
        result.started_at = now.strftime("%H:%M:%S")
        tag_base = f"{TAG_PREFIX}{now.strftime('%H%M%S')}{self._sweep_no % 100:02d}"   # 10 chars, alphanumeric
        log.info("Exit sweep %s start: reason=%s keys=%s", tag_base, result.reason, sorted(keys) if keys else "ALL")

        longs, shorts = self.long_rows()
        for r in shorts:
            if r["tradingsymbol"] not in self._shorts_reported:
                self._shorts_reported.add(r["tradingsymbol"])
                self.notify(f"Short position {r['tradingsymbol']} x{r['quantity']} seen: out of scope, left alone.", urgent=True)
        result.shorts = [r["tradingsymbol"] for r in shorts]
        if keys is not None:
            longs = [r for r in longs if r["key"] in keys]
        if self.s.skip_below_price > 0:
            cheap = [r for r in longs if r["last_price"] < self.s.skip_below_price]
            result.skipped = [f"{r['tradingsymbol']} (LTP {r['last_price']} below {self.s.skip_below_price})" for r in cheap]
            longs = [r for r in longs if r["last_price"] >= self.s.skip_below_price]
        if not longs:
            result.nothing_to_do = True
            result.elapsed = self.clock() - t0
            log.info("Exit sweep %s: nothing to do", tag_base)
            return result
        result.rows = longs

        # 2. cancel everything resting on these contracts (ours or not, including stop-losses)
        result.cancelled = self._cancel_resting(longs)

        # 3. place one LIMIT SELL per slice, capped at what is really open now
        net = self.broker.positions_net()
        orders = self.broker.orders()
        longs, _ = self.long_rows(net)
        if keys is not None:
            longs = [r for r in longs if r["key"] in keys]
        tracked: dict[str, dict] = {}
        part = 0
        for row in longs:
            qty = row["quantity"] - pending_sell_qty(orders, row, TAG_PREFIX)
            if qty_cap is not None:
                qty = min(qty, qty_cap)
            if qty <= 0:
                continue
            bid, ref = self._bid_and_ref(row)
            price = self._limit_price(bid, ref, row["tick_size"], attempt=0)
            for chunk in split_for_freeze(qty, row["lot_size"], self.s.freeze_qty.get(row["index"], 1000)):
                part += 1
                tag = f"{tag_base}{part:02d}"[:20]
                info = self._place(row, chunk, price, tag, result)
                if info is not None:
                    tracked[info["order_id"]] = info

        # 4-6. watch, re-price, confirm flat
        self._monitor(tracked, longs, keys, result, t0, capped=qty_cap is not None)
        result.elapsed = self.clock() - t0
        log.info("Exit sweep %s done: %s", tag_base, result.summary())
        return result

    # ------------------------------------------------------------ pieces
    def _cancel_resting(self, rows: list[dict]) -> int:
        try:
            orders = self.broker.orders()
        except Exception as exc:  # noqa: BLE001
            log.error("orders() failed before cancel: %s", exc)
            return 0
        targets = [o for o in orders if is_open_order(o) and any(same_contract(o, r) for r in rows)]
        ids = []
        for o in targets:
            try:
                self.broker.cancel(o["order_id"], o.get("variety") or "regular")
                ids.append(o["order_id"])
                self.record_order({"action": "cancel", "order_id": o["order_id"], "symbol": o.get("tradingsymbol"),
                                   "type": o.get("order_type"), "tag": o.get("tag"), "at": self._stamp()})
            except Exception as exc:  # noqa: BLE001 — it may have filled first; its pending qty still clamps
                log.warning("Cancel failed for %s: %s", o.get("order_id"), exc)
        waited = 0.0
        while ids and waited < self.s.cancel_wait_seconds:
            try:
                still = {x.get("order_id") for x in self.broker.orders() if is_open_order(x)}
            except Exception:  # noqa: BLE001
                break
            if still.isdisjoint(ids):
                break
            self.sleep(0.25)
            waited += 0.25
        return len(ids)

    def _bid_and_ref(self, row: dict) -> tuple[float, float]:
        key = f"{row['exchange']}:{row['tradingsymbol']}"
        bid = 0.0
        try:
            bid = self.broker.best_bid([key]).get(key, 0.0)
        except Exception as exc:  # noqa: BLE001
            log.warning("best_bid failed for %s: %s", key, exc)
        ltp = row.get("last_price") or 0.0
        try:
            ltp = self.broker.ltp([key]).get(key, ltp) or ltp
        except Exception:  # noqa: BLE001
            pass
        if bid <= 0:
            bid = ltp
        return bid, max(bid, ltp)

    def _limit_price(self, bid: float, ref: float, tick: float, attempt: int) -> float:
        """bid minus the buffer, a little more aggressive on each re-price, never below the
        exchange's LPP band and never below one tick."""
        buffer = self.s.exit_buffer_pct / 100 * (1 + 0.5 * attempt)
        raw = bid * (1 - buffer)
        price = max(round_to_tick(raw, tick), round_to_tick(lpp_floor(ref), tick), tick or 0.05)
        return price

    def _pace(self) -> None:
        with self._pace_lock:
            wait = self.s.order_gap_seconds - (self.clock() - self._last_order_at)
            if wait > 0:
                self.sleep(wait)
            self._last_order_at = self.clock()

    def _place(self, row: dict, qty: int, price: float, tag: str, result: SweepResult) -> dict | None:
        self._pace()
        info = {"order_id": "", "key": row["key"], "symbol": row["tradingsymbol"], "exchange": row["exchange"],
                "product": row["product"], "qty": qty, "price": price, "tag": tag, "type": "LIMIT",
                "attempts": 0, "filled": 0, "status": "", "placed_at": self.clock(), "at": self._stamp()}
        try:
            info["order_id"] = self.broker.place_limit_sell(row["exchange"], row["tradingsymbol"], row["product"], qty, price, tag)
        except Exception as exc:  # noqa: BLE001
            log.error("LIMIT SELL failed %s x%d @ %s: %s; trying MARKET with protection", row["tradingsymbol"], qty, price, exc)
            info["limit_error"] = str(exc)
            self._pace()
            try:
                info["order_id"] = self.broker.place_market_sell(row["exchange"], row["tradingsymbol"], row["product"], qty, tag,
                                                                 self.s.market_protection)
                info["type"] = "MARKET"
            except Exception as exc2:  # noqa: BLE001
                log.error("MARKET SELL failed %s x%d: %s", row["tradingsymbol"], qty, exc2)
                info["error"] = str(exc2)
                result.failed.append(f"{row['tradingsymbol']} x{qty}: {exc2}")
                self.record_order({"action": "place_failed", **info})
                return None
        log.info("Exit order placed: SELL %s x%d %s @ %s tag=%s id=%s", row["tradingsymbol"], qty, info["type"], price, tag, info["order_id"])
        self.record_order({"action": "place", **info})
        result.placed.append(info)
        return info

    def _monitor(self, tracked: dict[str, dict], rows: list[dict], keys: set[str] | None,
                 result: SweepResult, t0: float, capped: bool = False) -> None:
        """`capped`: a partial square-off (an add during lockout). Then 'done' means our
        orders are finished, not that the position is flat, and nothing is re-sent."""
        urgent_sent = False
        resent: set[str] = set()
        row_by_key = {r["key"]: r for r in rows}
        while True:
            self.sleep(self.s.reprice_interval_seconds)
            try:
                orders = {str(o.get("order_id")): o for o in self.broker.orders()}
                net = self.broker.positions_net()
            except Exception as exc:  # noqa: BLE001
                log.error("Read failed during sweep: %s", exc)
                continue
            longs, _ = self.long_rows(net)
            if keys is not None:
                longs = [r for r in longs if r["key"] in keys]
            open_keys = {r["key"] for r in longs}
            # update tracked orders from the order book
            for oid, info in tracked.items():
                o = orders.get(oid)
                if o is None:
                    continue
                info["status"] = o.get("status", "")
                info["filled"] = int(o.get("filled_quantity") or 0)
                info["avg_price"] = float(o.get("average_price") or 0)
                if info["status"] in TERMINAL_STATUSES and not info.get("done"):
                    info["done"] = True
                    self.record_order({"action": "result", "order_id": oid, "symbol": info["symbol"], "status": info["status"],
                                       "filled": info["filled"], "avg_price": info["avg_price"], "tag": info["tag"], "at": self._stamp()})
                    if info["status"] == "REJECTED":
                        result.failed.append(f"{info['symbol']} x{info['qty']}: rejected {o.get('status_message') or ''}".strip())
            # flat?
            if not open_keys:
                result.still_open = []
                return
            if capped and all(info.get("done") for info in tracked.values()):
                unfilled = [f"{i['symbol']} x{i['qty'] - i['filled']}" for i in tracked.values() if i["filled"] < i["qty"]]
                result.still_open = unfilled
                return
            elapsed = self.clock() - t0
            # re-price orders still open on positions still open
            for oid, info in tracked.items():
                o = orders.get(oid)
                if o is None or not is_open_order(o) or info["key"] not in open_keys:
                    continue
                if info["attempts"] >= self.s.reprice_attempts or info["type"] != "LIMIT":
                    continue
                row = row_by_key.get(info["key"])
                if row is None:
                    continue
                bid, ref = self._bid_and_ref(row)
                info["attempts"] += 1
                new_price = min(info["price"], self._limit_price(bid, ref, row["tick_size"], info["attempts"]))
                if new_price < info["price"]:
                    try:
                        self.broker.modify_price(oid, new_price, o.get("variety") or "regular")
                        log.info("Re-priced %s %s: %s -> %s (attempt %d)", oid, info["symbol"], info["price"], new_price, info["attempts"])
                        self.record_order({"action": "modify", "order_id": oid, "symbol": info["symbol"], "price": new_price,
                                           "attempt": info["attempts"], "at": self._stamp()})
                        info["price"] = new_price
                    except Exception as exc:  # noqa: BLE001
                        log.error("Modify failed for %s: %s", oid, exc)
            # a position with no live order of ours (rejected, or cancelled by someone): one re-send
            live_keys = {info["key"] for oid, info in tracked.items() if (orders.get(oid) or {}).get("status") not in TERMINAL_STATUSES}
            for r in longs:
                if capped or r["key"] in live_keys or r["key"] in resent:
                    continue
                resent.add(r["key"])
                qty = r["quantity"] - pending_sell_qty(list(orders.values()), r, TAG_PREFIX)
                if qty <= 0:
                    continue
                bid, ref = self._bid_and_ref(r)
                price = self._limit_price(bid, ref, r["tick_size"], attempt=1)
                tag = f"{TAG_PREFIX}R{self.now_fn().strftime('%H%M%S')}{len(tracked) % 100:02d}"[:20]
                log.warning("No live exit order for %s (x%d); re-sending once", r["tradingsymbol"], qty)
                for chunk in split_for_freeze(qty, r["lot_size"], self.s.freeze_qty.get(r["index"], 1000)):
                    info = self._place(r, chunk, price, tag, result)
                    if info is not None:
                        tracked[info["order_id"]] = info
                row_by_key[r["key"]] = r
            if elapsed >= self.s.urgent_after_seconds:
                result.still_open = [f"{r['tradingsymbol']} x{r['quantity']}" for r in longs]
                if not urgent_sent:
                    urgent_sent = True
                    self.notify("Exit incomplete after %ds. Still open: %s. EXIT MANUALLY."
                                % (int(elapsed), ", ".join(result.still_open)), urgent=True)
                # keep trying re-prices for a while, then hand back to the caller
                if elapsed >= self.s.urgent_after_seconds * 2:
                    return

    def _stamp(self) -> str:
        return self.now_fn().strftime("%H:%M:%S")
