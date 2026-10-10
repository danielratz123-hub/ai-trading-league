"""Live prices for the dashboard: ONE Alpaca market-data WebSocket, fanned out
to browsers with Server-Sent Events (/api/stream). Display only.

    Alpaca  --(1 WebSocket, IEX feed)-->  this process  --(SSE)-->  browsers

- One connection, held by the dashboard (api) service only: Alpaca's free
  plan allows a single stream connection per account (the setup service
  runs with ARENA_LIVE=0).
- Subscribes to the trades of every ticker either agent holds, plus SPY.
  The free plan allows 30 symbols: the 30 largest positions stream, the rest
  are polled through the REST API every 60 s (one batched call: well under
  Alpaca's 200 calls/min). Holdings are re-read every 60 s, so new positions
  are picked up right after an execute.
- Connected only during market hours (from the trading calendar). Drops are
  retried with exponential backoff (1 s, 2 s, 4 s ... 60 s, plus jitter).
- Browsers never talk to Alpaca and never see a key: they get ticker/price
  pairs from this server.
- Nothing here is written to the database. Scoring, the ledger and official
  equity keep using the engine's daily marks.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from ..config import SETTINGS
from ..prices import ET

log = logging.getLogger("arena.live")

STREAM_URL = "wss://stream.data.alpaca.markets/v2/{feed}"
MAX_STREAM_SYMBOLS = 30          # Alpaca free plan
REPLAN_SECONDS = 60              # re-read holdings / poll the overflow
FLUSH_SECONDS = 1.0              # batch price events to browsers
MAX_BACKOFF = 60.0


def market_hours(conn, now: datetime) -> tuple[bool, str | None]:
    """(is the market open now, today's close 'HH:MM'), from the trading calendar."""
    row = conn.execute("SELECT open_time, close_time FROM calendar WHERE date=?",
                       (now.date().isoformat(),)).fetchone()
    if row is None:
        return False, None
    hm = now.strftime("%H:%M")
    return row[0] <= hm < row[1], row[1]


def plan_symbols(holdings: dict[str, float], limit: int = MAX_STREAM_SYMBOLS) -> tuple[list[str], list[str]]:
    """(streamed, polled). holdings: ticker -> |position value| (summed over
    agents). SPY always streams; then the largest positions, up to `limit`."""
    rest = sorted((t for t in holdings if t != "SPY"), key=lambda t: (-holdings[t], t))
    streamed = ["SPY"] + rest[: limit - 1]
    return sorted(streamed), sorted(rest[limit - 1:])


class Hub:
    """Fan-out to SSE clients. The stream thread publishes; each browser
    connection has an asyncio.Queue on the web server's event loop."""

    def __init__(self):
        self._clients: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = set()
        self._lock = threading.Lock()

    def add(self, loop, queue) -> None:
        with self._lock:
            self._clients.add((loop, queue))

    def remove(self, loop, queue) -> None:
        with self._lock:
            self._clients.discard((loop, queue))

    def count(self) -> int:
        return len(self._clients)

    def publish(self, event: dict) -> None:
        with self._lock:
            clients = list(self._clients)
        for loop, q in clients:
            def put(q=q):
                if q.full():                     # a slow browser loses old events, never blocks us
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                q.put_nowait(event)
            try:
                loop.call_soon_threadsafe(put)
            except RuntimeError:                 # that loop is closed
                self.remove(loop, q)


class LiveStream:
    def __init__(self, db_path_fn, feed: str | None = None, url: str | None = None,
                 rest_latest=None, connect=None, clock=None):
        self.db_path_fn = db_path_fn
        self.feed = (feed or SETTINGS.data_feed).lower()
        self.url = url or STREAM_URL.format(feed=self.feed)
        self.rest_latest = rest_latest            # callable(list[str]) -> {ticker: (price, iso time)}
        self._connect = connect
        self.clock = clock or (lambda: datetime.now(ET))
        self.hub = Hub()
        self.quotes: dict[str, float] = {}
        self.quote_time: dict[str, str] = {}
        self.status = "starting"                  # live | closed | connecting | reconnecting | error | disabled
        self.error: str | None = None
        self.market_open = False
        self.streamed: list[str] = []
        self.polled: list[str] = []
        self._pending: dict[str, float] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------ public

    def enabled(self) -> bool:
        return os.getenv("ARENA_LIVE", "1") != "0" and bool(os.getenv("ALPACA_API_KEY"))

    def start(self) -> None:
        if self._thread or not self.enabled():
            if not self.enabled():
                self.status = "disabled"
            return
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), name="live-stream", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def state(self) -> dict:
        """For /api/live and the first SSE message. No secrets, by design."""
        last = max(self.quote_time.values(), default=None)
        return {"enabled": self.status != "disabled", "market_open": self.market_open, "status": self.status,
                "feed": self.feed, "label": f"Live · {self.feed.upper()}" if self.status == "live" else None,
                "quotes": dict(self.quotes), "updated": _hhmm(last), "as_of": last,
                "streamed": list(self.streamed), "polled": list(self.polled), "error": self.error}

    # ------------------------------------------------------------------ internals

    def _read_db(self) -> tuple[bool, dict[str, float]]:
        path = Path(self.db_path_fn())
        if not path.exists():
            return False, {}
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
        try:
            open_, _ = market_hours(conn, self.clock())
            holdings: dict[str, float] = {}
            for t, sh in conn.execute("SELECT ticker, SUM(ABS(shares)) FROM positions GROUP BY ticker"):
                row = conn.execute("SELECT close FROM prices WHERE ticker=? ORDER BY date DESC LIMIT 1", (t,)).fetchone()
                holdings[t] = (sh or 0) * (self.quotes.get(t) or (row[0] if row else 0) or 0)
            return open_, holdings
        finally:
            conn.close()

    def _set_status(self, status: str, error: str | None = None) -> None:
        if (status, error) != (self.status, self.error):
            self.status, self.error = status, error
            self.hub.publish({"type": "status", **{k: v for k, v in self.state().items() if k != "quotes"}})

    def _record(self, ticker: str, price: float, ts: str | None) -> None:
        self.quotes[ticker] = price
        self.quote_time[ticker] = (_parse(ts) or self.clock()).isoformat()   # ET, comparable
        self._pending[ticker] = price

    def _flush(self) -> None:
        if self._pending:
            batch, self._pending = self._pending, {}
            self.hub.publish({"type": "prices", "quotes": batch, "updated": _hhmm(max(self.quote_time.values()))})

    def _poll(self, symbols: list[str]) -> None:
        if not symbols:
            return
        try:
            for t, (price, ts) in self._rest(symbols).items():
                self._record(t, price, ts)
        except Exception as e:  # noqa: BLE001 - polling is best-effort
            log.warning("REST price poll failed (%s)", type(e).__name__)

    def _rest(self, symbols: list[str]) -> dict[str, tuple[float, str]]:
        if self.rest_latest is None:
            from alpaca.data.enums import DataFeed
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockLatestTradeRequest
            client = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])

            def latest(syms):
                res = client.get_stock_latest_trade(StockLatestTradeRequest(
                    symbol_or_symbols=syms, feed=DataFeed(self.feed)))
                return {s: (float(t.price), t.timestamp.astimezone(ET).isoformat()) for s, t in res.items()}
            self.rest_latest = latest
        return self.rest_latest(symbols)

    async def _main(self) -> None:
        self.loop = asyncio.get_running_loop()
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self.market_open, holdings = self._read_db()
                if not self.market_open:
                    self._set_status("closed")
                    await asyncio.sleep(30)
                    continue
                self._set_status("connecting" if self.status != "reconnecting" else "reconnecting")
                await self._session(holdings)
                backoff = 1.0                     # a clean end (market closed)
            except Exception as e:  # noqa: BLE001 - never let the dashboard die with the stream
                msg = f"{type(e).__name__}: {str(e)[:160]}"
                log.warning("live stream dropped (%s); retrying in %.0fs", msg, backoff)
                self._set_status("reconnecting", msg)
                await asyncio.sleep(backoff + random.uniform(0, backoff / 4))
                backoff = min(MAX_BACKOFF, backoff * 2)

    async def _session(self, holdings: dict[str, float]) -> None:
        if self._connect is None:
            from websockets.asyncio.client import connect
            self._connect = connect
        streamed, polled = plan_symbols(holdings)
        self._poll(streamed + polled)             # one REST call: a price for everything right away
        self._flush()
        async with self._connect(self.url, open_timeout=20, ping_interval=20, ping_timeout=20) as ws:
            await self._expect(ws, "connected")
            await ws.send(json.dumps({"action": "auth", "key": os.environ.get("ALPACA_API_KEY", ""),
                                      "secret": os.environ.get("ALPACA_SECRET_KEY", "")}))
            await self._expect(ws, "authenticated")
            await ws.send(json.dumps({"action": "subscribe", "trades": streamed}))
            self.streamed, self.polled = streamed, polled
            self._set_status("live")
            next_plan, next_flush = time.monotonic() + REPLAN_SECONDS, time.monotonic() + FLUSH_SECONDS
            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=FLUSH_SECONDS)
                    self._handle(raw)
                except asyncio.TimeoutError:
                    pass
                now = time.monotonic()
                if now >= next_flush:
                    self._flush()
                    next_flush = now + FLUSH_SECONDS
                if now >= next_plan:
                    next_plan = now + REPLAN_SECONDS
                    self.market_open, holdings = self._read_db()
                    if not self.market_open:
                        self._flush()
                        self._set_status("closed")
                        return
                    await self._replan(ws, holdings)

    async def _replan(self, ws, holdings: dict[str, float]) -> None:
        streamed, polled = plan_symbols(holdings)
        add = sorted(set(streamed) - set(self.streamed))
        drop = sorted(set(self.streamed) - set(streamed))
        if drop:
            await ws.send(json.dumps({"action": "unsubscribe", "trades": drop}))
        if add:
            await ws.send(json.dumps({"action": "subscribe", "trades": add}))
            self._poll(add)                       # a price now, before the first trade arrives
        self.streamed, self.polled = streamed, polled
        self._poll(polled)

    async def _expect(self, ws, msg: str) -> None:
        raw = await asyncio.wait_for(ws.recv(), timeout=20)
        for m in json.loads(raw):
            if m.get("T") == "error":
                raise ConnectionError(f"Alpaca stream error {m.get('code')}: {m.get('msg')}")
            if m.get("T") == "success" and m.get("msg") == msg:
                return
        raise ConnectionError(f"Alpaca stream: expected {msg!r}")

    def _handle(self, raw) -> None:
        for m in json.loads(raw):
            t = m.get("T")
            if t == "t" and m.get("S") and m.get("p") is not None:
                self._record(m["S"], float(m["p"]), m.get("t"))
            elif t == "error":
                raise ConnectionError(f"Alpaca stream error {m.get('code')}: {m.get('msg')}")


SSE_PING_SECONDS = 15


async def sse_events(src, queue: asyncio.Queue, is_disconnected, ping: float | None = None):
    """The /api/stream body: a 'snapshot', then 'prices' batches and 'status'
    changes, with a keep-alive comment when idle. Ends when the browser
    leaves. Ticker/price pairs and status only: never a key."""
    first = src.state() if src else {"enabled": False, "status": "disabled", "quotes": {}}
    yield f"event: snapshot\ndata: {json.dumps(first)}\n\n"
    while not await is_disconnected():
        try:
            ev = await asyncio.wait_for(queue.get(), timeout=ping or SSE_PING_SECONDS)
        except asyncio.TimeoutError:
            yield ": keep-alive\n\n"            # keeps proxies from closing an idle stream
            continue
        yield f"event: {ev.get('type', 'message')}\ndata: {json.dumps(ev)}\n\n"


def _parse(ts) -> datetime | None:
    """Alpaca times are RFC 3339, sometimes with nanoseconds ('...44.208123456Z');
    Python accepts at most microseconds. Returns an ET datetime."""
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts.astimezone(ET)
    s = str(ts).replace("Z", "+00:00")
    if "." in s:
        head, rest = s.split(".", 1)
        n = 0
        while n < len(rest) and rest[n].isdigit():      # only the digits of the fraction
            n += 1
        s = f"{head}.{rest[:n][:6]}{rest[n:]}"
    try:
        return datetime.fromisoformat(s).astimezone(ET)
    except ValueError:
        return None


def _hhmm(iso: str | None) -> str | None:
    d = _parse(iso)
    return d.strftime("%H:%M") if d else None


LIVE: LiveStream | None = None
