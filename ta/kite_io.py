"""
Kite session: REST client with daily-token hot reload, and the ticker websocket.

The access token file is written each morning by /opt/pnl-monitor/auto_token.py. This
re-reads it every 30 s and swaps the REST token and the ticker in place, so the
service never needs a restart for a new token (pattern from trading-dashboard/server.py).
Order updates and ticks are handed to the service via callbacks that run on the
ticker's thread; the service keeps them cheap (append to a queue / update a dict).
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("ta.kite")


class KiteSession:
    def __init__(self, api_key: str, token_path, on_ticks, on_order_update, clock=time.time):
        from kiteconnect import KiteConnect  # imported here so tests never need the package
        self.api_key = api_key
        self.token_path = token_path
        self.kite = KiteConnect(api_key=api_key)
        self.on_ticks = on_ticks
        self.on_order_update = on_order_update
        self.clock = clock
        self.token = ""
        self.rejected_token = ""
        self.kws = None
        self.ticker_connected = False
        self.last_tick_at = 0.0
        self.last_token_check = 0.0
        self._subscribed: set[int] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------ token
    def read_token_file(self) -> str:
        try:
            return self.token_path.read_text().strip()
        except FileNotFoundError:
            return ""

    def ensure_session(self, verify=None) -> tuple[bool, str]:
        """Make sure the REST client holds the current token. Returns (ok, message).
        `verify` is a callable that makes a cheap authenticated call (e.g. profile)."""
        token = self.read_token_file()
        if not token:
            return False, f"no access token at {self.token_path}"
        if token == self.rejected_token:
            return False, "token rejected by Kite; waiting for a new token file"
        if token == self.token:
            return True, "ok"
        self.kite.set_access_token(token)
        if verify is not None:
            try:
                verify()
            except Exception as exc:  # noqa: BLE001
                name = type(exc).__name__
                if "Token" in name or "403" in str(exc):
                    self.rejected_token = token
                    return False, f"Kite rejected the access token ({exc})"
                return False, f"Kite not reachable: {exc}"
        changed = self.token != ""
        self.token = token
        self.rejected_token = ""
        log.info("Access token %s", "refreshed" if changed else "loaded")
        self.start_ticker()
        return True, "token refreshed" if changed else "token loaded"

    # ------------------------------------------------------------ ticker
    def start_ticker(self) -> None:
        from kiteconnect import KiteTicker
        old = self.kws
        new = KiteTicker(self.api_key, self.token)
        new.on_connect = self._on_connect
        new.on_ticks = self._on_ticks
        new.on_close = self._on_close
        new.on_error = self._on_error
        new.on_reconnect = self._on_reconnect
        new.on_noreconnect = self._on_noreconnect
        new.on_order_update = self._on_order_update
        self.kws = new
        self.ticker_connected = False
        if old is not None:
            self._on_ticker_thread(old.close)
        new.connect(threaded=True)

    def _on_ticker_thread(self, fn, *args) -> None:
        """Twisted is not thread-safe: once the reactor runs, socket calls go through it."""
        try:
            from twisted.internet import reactor
            if reactor.running:
                reactor.callFromThread(fn, *args)
                return
        except Exception:  # noqa: BLE001
            pass
        fn(*args)

    def subscribe(self, tokens: set[int]) -> None:
        """Subscribe (LTP mode) to exactly these instrument tokens."""
        with self._lock:
            add = tokens - self._subscribed
            remove = self._subscribed - tokens
            self._subscribed = set(tokens)
        if self.kws is None or not self.ticker_connected:
            return
        ws = self.kws
        if remove:
            self._on_ticker_thread(ws.unsubscribe, sorted(remove))
        if add:
            def _sub():
                ws.subscribe(sorted(add))
                ws.set_mode(ws.MODE_LTP, sorted(add))
            self._on_ticker_thread(_sub)

    # ---- callbacks (ticker thread)
    def _on_connect(self, ws, response):
        if ws is not self.kws:
            return
        self.ticker_connected = True
        with self._lock:
            tokens = sorted(self._subscribed)
        if tokens:
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_LTP, tokens)
        log.info("Ticker connected (%d instruments)", len(tokens))

    def _on_ticks(self, ws, ticks):
        if ws is not self.kws:
            return
        self.last_tick_at = self.clock()
        try:
            self.on_ticks(ticks)
        except Exception as exc:  # noqa: BLE001
            log.error("on_ticks handler error: %s", exc)

    def _on_order_update(self, ws, data):
        if ws is not self.kws:
            return
        try:
            self.on_order_update(data)
        except Exception as exc:  # noqa: BLE001
            log.error("on_order_update handler error: %s", exc)

    def _on_close(self, ws, code, reason):
        if ws is self.kws:
            self.ticker_connected = False
            log.warning("Ticker closed: %s %s", code, reason)

    def _on_error(self, ws, code, reason):
        if ws is self.kws:
            log.error("Ticker error: %s %s", code, reason)

    def _on_reconnect(self, ws, attempt):
        if ws is self.kws:
            self.ticker_connected = False
            log.warning("Ticker reconnecting (attempt %s)", attempt)

    def _on_noreconnect(self, ws):
        if ws is self.kws:
            self.ticker_connected = False
            log.error("Ticker gave up reconnecting; a fresh ticker starts on the next token check")
            self.token = ""   # forces ensure_session to rebuild the ticker
