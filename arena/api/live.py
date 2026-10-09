"""Live prices for the dashboard, during market hours.

A background thread fetches the latest price of every held ticker (+ SPY)
from Alpaca every 2 minutes while the market is open, and keeps them in
memory. Nothing is written to the database: live values are for watching
only. The official numbers (scores, snapshots) still come from the daily
closing prices.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

from ..prices import ET

log = logging.getLogger("arena.live")
REFRESH_SECONDS = 120


def market_hours(conn, now: datetime) -> tuple[bool, str | None]:
    """(is the market open now, today's close 'HH:MM'), from the trading calendar."""
    row = conn.execute("SELECT open_time, close_time FROM calendar WHERE date=?",
                       (now.date().isoformat(),)).fetchone()
    if row is None:
        return False, None
    hm = now.strftime("%H:%M")
    return row[0] <= hm < row[1], row[1]


class LivePrices:
    def __init__(self, db_path_fn, source_factory=None, refresh_seconds: int = REFRESH_SECONDS):
        self.db_path_fn = db_path_fn
        self.source_factory = source_factory
        self.refresh_seconds = refresh_seconds
        self.quotes: dict[str, float] = {}
        self.updated: datetime | None = None
        self.market_open = False
        self.error: str | None = None
        self.feed = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def enabled(self) -> bool:
        return os.getenv("ARENA_LIVE", "1") != "0" and bool(os.getenv("ALPACA_API_KEY"))

    def start(self) -> None:
        if self._thread or not self.enabled():
            return
        self._thread = threading.Thread(target=self._loop, name="live-prices", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception as e:  # noqa: BLE001 - live prices are best-effort; never crash the app
                self.error = f"{type(e).__name__}: {str(e)[:200]}"
                log.warning("live price refresh failed: %s", self.error)
            self._stop.wait(self.refresh_seconds)

    def refresh(self, now: datetime | None = None) -> None:
        now = now or datetime.now(ET)
        path = Path(self.db_path_fn())
        if not path.exists():
            return
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            self.market_open, _ = market_hours(conn, now)
            if not self.market_open:
                return
            tickers = sorted({r[0] for r in conn.execute("SELECT DISTINCT ticker FROM positions")} | {"SPY"})
        finally:
            conn.close()
        if self.source_factory is None:
            from ..prices import AlpacaPriceSource
            self.source_factory = AlpacaPriceSource
        # One price source for the life of the app (one Alpaca client, and it
        # remembers which data feed works).
        if getattr(self, "_source", None) is None:
            self._source = self.source_factory()
        source = self._source
        latest = source.fetch_latest(tickers, now - timedelta(minutes=30))
        # Keep earlier quotes for tickers that didn't trade in the last 30 minutes.
        self.quotes = {**self.quotes, **{r.ticker: float(r.price) for r in latest.itertuples()}}
        self.updated = now
        self.feed = getattr(source, "feed", "")
        self.error = None

    def state(self) -> dict:
        return {"enabled": self.enabled(), "market_open": self.market_open, "quotes": dict(self.quotes),
                "updated": self.updated.strftime("%H:%M") if self.updated else None,
                "feed": self.feed, "error": self.error}


LIVE: LivePrices | None = None
