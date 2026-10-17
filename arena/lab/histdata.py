"""Historical daily bars for the backtest lab, in their own SQLite file.

data/history.db holds split- and dividend-adjusted SIP daily bars from 2016
for the league universe + SPY. It is never mixed with the live `prices`
table (the league trades on IEX bars, which only go back to ~2020), and it
is not in the nightly backup: everything here can be downloaded again.

    download()   first fill; resumable: progress is saved per batch of
                 symbols, so a crash or a rate-limit error picks up where it stopped
    update()     nightly append of the latest day(s); a split/dividend
                 (overlap mismatch) re-downloads that symbol's history
    gap_check()  every symbol's bars against the exchange calendar

Tables: hist_bars_daily, hist_calendar, hist_symbols (download state),
hist_meta (feed, last update, the survivorship note).
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Protocol

import pandas as pd

from ..prices import BAR_COLUMNS, ET, detect_adjustment
from . import config as labconfig

log = logging.getLogger("arena.lab.history")

SCHEMA = """
CREATE TABLE IF NOT EXISTS hist_bars_daily (
    symbol  TEXT NOT NULL,
    day     TEXT NOT NULL,          -- YYYY-MM-DD, US/Eastern trading day
    open    REAL NOT NULL,
    high    REAL NOT NULL,
    low     REAL NOT NULL,
    close   REAL NOT NULL,
    volume  REAL NOT NULL,
    PRIMARY KEY (symbol, day)
);
CREATE INDEX IF NOT EXISTS idx_hist_bars_day ON hist_bars_daily(day);

-- The exchange calendar (Alpaca), so gaps can be told from holidays.
CREATE TABLE IF NOT EXISTS hist_calendar (
    day        TEXT PRIMARY KEY,
    open_time  TEXT NOT NULL,
    close_time TEXT NOT NULL
);

-- Download progress per symbol (what makes download() resumable).
CREATE TABLE IF NOT EXISTS hist_symbols (
    symbol     TEXT PRIMARY KEY,
    status     TEXT NOT NULL CHECK (status IN ('pending', 'done', 'no_data', 'failed')),
    first_day  TEXT,
    last_day   TEXT,
    rows       INTEGER NOT NULL DEFAULT 0,
    attempts   INTEGER NOT NULL DEFAULT 0,
    error      TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hist_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    path = str(path or labconfig.history_db_path())
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    set_meta(conn, "survivorship_note", labconfig.get().survivorship_note)
    conn.commit()
    return conn


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO hist_meta(key, value) VALUES (?,?)", (key, str(value)))


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM hist_meta WHERE key=?", (key,)).fetchone()
    return None if row is None else row[0]


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------

class HistorySource(Protocol):
    def fetch(self, symbols: list[str], start: str, end: str) -> pd.DataFrame:
        """BAR_COLUMNS (ticker, date, open, high, low, close, volume) for start..end inclusive.
        Raises on any error (the caller retries)."""
        ...

    def calendar(self, start: str, end: str) -> list[tuple[str, str, str]]:
        """[(day, open 'HH:MM', close 'HH:MM')]"""
        ...


class AlpacaHistorySource:
    """Adjusted daily bars from Alpaca, one feed only (no silent IEX fallback:
    IEX history starts ~2020, so falling back would leave holes)."""

    def __init__(self, feed: str | None = None):
        from ..prices import AlpacaPriceSource
        self.feed = feed or labconfig.get().history.feed
        self._src = AlpacaPriceSource(self.feed)

    def fetch(self, symbols: list[str], start: str, end: str) -> pd.DataFrame:
        from ..prices import _drop_unfinished_today, _normalize_alpaca
        raw = self._src._request(symbols, start, end, self.feed)
        if raw is None or raw.empty:
            return pd.DataFrame(columns=BAR_COLUMNS)
        return _drop_unfinished_today(_normalize_alpaca(raw))

    def calendar(self, start: str, end: str) -> list[tuple[str, str, str]]:
        from ..trading_calendar import fetch_from_alpaca
        return fetch_from_alpaca(start, end)


class RateLimiter:
    """At most `per_minute` calls: waits so calls are evenly spaced."""

    def __init__(self, per_minute: int, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.interval = 60.0 / max(per_minute, 1)
        self.sleep, self.clock = sleep, clock
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            gap = self.interval - (self.clock() - self._last)
            if gap > 0:
                self.sleep(gap)
        self._last = self.clock()


def _with_retries(fn: Callable[[], pd.DataFrame], limiter: RateLimiter, retries: int,
                  sleep: Callable[[float], None]) -> pd.DataFrame:
    """Rate-limited call; on error wait 2, 4, 8 ... seconds (max 60) and retry."""
    for attempt in range(retries + 1):
        limiter.wait()
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - the SDK raises several types
            if attempt == retries:
                raise
            delay = min(2 ** (attempt + 1), 60)
            log.warning("history request failed (%s: %s); retry %d/%d in %ss",
                        type(exc).__name__, str(exc)[:200], attempt + 1, retries, delay)
            sleep(delay)
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------
# Storing
# --------------------------------------------------------------------------

def _insert(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    rows = [(r.ticker, r.date, float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume))
            for r in df.itertuples(index=False)]
    conn.executemany("INSERT OR REPLACE INTO hist_bars_daily(symbol, day, open, high, low, close, volume) "
                     "VALUES (?,?,?,?,?,?,?)", rows)
    return len(rows)


def _refresh_symbol(conn: sqlite3.Connection, symbol: str, status: str | None = None,
                    error: str | None = None) -> None:
    first, last, n = conn.execute("SELECT MIN(day), MAX(day), COUNT(*) FROM hist_bars_daily WHERE symbol=?",
                                  (symbol,)).fetchone()
    if status is None:
        status = "done" if n else "no_data"
    conn.execute(
        "INSERT INTO hist_symbols(symbol, status, first_day, last_day, rows, attempts, error, updated_at) "
        "VALUES (?,?,?,?,?,1,?,?) ON CONFLICT(symbol) DO UPDATE SET status=excluded.status, "
        "first_day=excluded.first_day, last_day=excluded.last_day, rows=excluded.rows, "
        "attempts=hist_symbols.attempts+1, error=excluded.error, updated_at=excluded.updated_at",
        (symbol, status, first, last, n, error, _now()))


def store_calendar(conn: sqlite3.Connection, source: HistorySource, start: str, end: str) -> int:
    days = source.calendar(start, end)
    conn.executemany("INSERT OR REPLACE INTO hist_calendar(day, open_time, close_time) VALUES (?,?,?)", days)
    conn.commit()
    return len(days)


def calendar_days(conn: sqlite3.Connection, start: str | None = None, end: str | None = None) -> list[str]:
    return [r[0] for r in conn.execute("SELECT day FROM hist_calendar WHERE day >= ? AND day <= ? ORDER BY day",
                                       (start or "0000", end or "9999"))]


def last_completed_day(conn: sqlite3.Connection, now: datetime | None = None) -> str | None:
    """The latest trading day whose daily bar is final (after 16:30 ET)."""
    now = (now or datetime.now(ET)).astimezone(ET)
    today = now.date().isoformat()
    days = calendar_days(conn, end=today)
    if days and days[-1] == today and (now.hour, now.minute) < (16, 30):
        days = days[:-1]
    return days[-1] if days else None


def universe_symbols(extra: Iterable[str] = ()) -> list[str]:
    """The league universe (S&P 500 + its ETFs; SPY is one of them) plus extras."""
    from .. import universe
    return sorted(set(universe.ALL_TICKERS) | {universe.BENCHMARK} | set(labconfig.get().history.extra_symbols)
                  | {s.upper() for s in extra})


# --------------------------------------------------------------------------
# Download / update
# --------------------------------------------------------------------------

@dataclass
class DownloadReport:
    requested: int = 0
    skipped_done: int = 0
    rows: int = 0
    done: list[str] = field(default_factory=list)
    no_data: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    redownloaded: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"requested": self.requested, "skipped_done": self.skipped_done, "rows": self.rows,
                "done": len(self.done), "no_data": self.no_data, "failed": self.failed,
                "redownloaded": self.redownloaded}


def _status(conn: sqlite3.Connection) -> dict[str, str]:
    return {r[0]: r[1] for r in conn.execute("SELECT symbol, status FROM hist_symbols")}


def download(conn: sqlite3.Connection, source: HistorySource, symbols: list[str], end: str,
             start: str | None = None, force: bool = False, limiter: RateLimiter | None = None,
             sleep: Callable[[float], None] = time.sleep) -> DownloadReport:
    """Full history for every symbol not yet done. Saved after each batch."""
    h = labconfig.get().history
    start = start or h.start
    limiter = limiter or RateLimiter(h.requests_per_minute, sleep)
    rep = DownloadReport(requested=len(symbols))
    status = _status(conn)
    todo = [s for s in sorted(set(symbols)) if force or status.get(s) not in ("done", "no_data")]
    rep.skipped_done = len(set(symbols)) - len(todo)
    for i in range(0, len(todo), h.batch_symbols):
        batch = todo[i:i + h.batch_symbols]
        try:
            df = _with_retries(lambda b=batch: source.fetch(b, start, end), limiter, h.max_retries, sleep)
        except Exception as exc:  # noqa: BLE001
            msg = f"{type(exc).__name__}: {str(exc)[:300]}"
            for s in batch:
                _refresh_symbol(conn, s, "failed", msg)
                rep.failed[s] = msg
            conn.commit()
            continue
        df = df[df["ticker"].isin(batch) & (df["date"] >= start) & (df["date"] <= end)]
        got = set(df["ticker"])
        if force:
            # Replace only what came back: an empty answer never wipes stored history.
            conn.executemany("DELETE FROM hist_bars_daily WHERE symbol=?", [(s,) for s in got])
        rep.rows += _insert(conn, df)
        for s in batch:
            _refresh_symbol(conn, s)
            stored = conn.execute("SELECT status FROM hist_symbols WHERE symbol=?", (s,)).fetchone()[0]
            (rep.done if stored == "done" else rep.no_data).append(s)
        conn.commit()
        log.info("history: %d/%d symbols", min(i + h.batch_symbols, len(todo)), len(todo))
    set_meta(conn, "feed", h.feed)
    set_meta(conn, "last_download", _now())
    conn.commit()
    return rep


def update(conn: sqlite3.Connection, source: HistorySource, symbols: list[str], end: str,
           limiter: RateLimiter | None = None, sleep: Callable[[float], None] = time.sleep) -> DownloadReport:
    """Nightly: append new days; a symbol whose overlapping closes changed was
    split/paid a dividend (adjusted history is rescaled), so it is re-downloaded."""
    h = labconfig.get().history
    limiter = limiter or RateLimiter(h.requests_per_minute, sleep)
    status = _status(conn)
    new = [s for s in symbols if status.get(s) not in ("done", "no_data")]
    rep = download(conn, source, new, end, limiter=limiter, sleep=sleep) if new else DownloadReport()
    rep.requested = len(symbols)
    last = {r[0]: r[1] for r in conn.execute("SELECT symbol, last_day FROM hist_symbols WHERE status='done'")}
    groups: dict[str, list[str]] = {}
    for s in symbols:
        if s in last and last[s] and last[s] < end:
            start = (date.fromisoformat(last[s]) - timedelta(days=h.overlap_days)).isoformat()
            groups.setdefault(start, []).append(s)
    adjusted: dict[str, float] = {}
    for start, group in sorted(groups.items()):
        for i in range(0, len(group), h.batch_symbols):
            batch = group[i:i + h.batch_symbols]
            try:
                recent = _with_retries(lambda b=batch, st=start: source.fetch(b, st, end), limiter,
                                       h.max_retries, sleep)
            except Exception as exc:  # noqa: BLE001
                for s in batch:
                    rep.failed[s] = f"{type(exc).__name__}: {str(exc)[:300]}"
                continue
            for s in batch:
                new_s = recent[recent["ticker"] == s]
                if new_s.empty:
                    continue
                stored = pd.read_sql_query("SELECT day AS date, close FROM hist_bars_daily WHERE symbol=? AND day>=?",
                                           conn, params=(s, start)).set_index("date")["close"]
                factor = detect_adjustment(stored, new_s.set_index("date")["close"])
                if factor is not None:
                    adjusted[s] = factor
                    continue
                rep.rows += _insert(conn, new_s[new_s["date"] > last[s]])
                _refresh_symbol(conn, s)
            conn.commit()
    if adjusted:
        again = download(conn, source, sorted(adjusted), end, force=True, limiter=limiter, sleep=sleep)
        rep.rows += again.rows
        rep.failed.update(again.failed)
        rep.redownloaded = adjusted
    set_meta(conn, "last_update", _now())
    set_meta(conn, "last_update_through", end)
    conn.commit()
    return rep


# --------------------------------------------------------------------------
# Gap check
# --------------------------------------------------------------------------

def gap_check(conn: sqlite3.Connection, through: str | None = None, max_listed: int = 50) -> dict:
    """Every symbol's days against the calendar.

    missing     calendar days between a symbol's first and last bar with no bar: a real gap
    stale       the symbol's last bar is before `through` (stopped trading / renamed / delisted)
    late_start  first bar after the history start: a listing (or data start), not a gap
    off_calendar bars on days the calendar says were closed
    no_data / failed  symbols the download got nothing for

    ok = SPY present from the first calendar day, nothing missing, nothing
    off-calendar and nothing failed. Stale and no-data symbols are reported
    (a delisting, merger or rename, like a listing) but are not gaps.
    """
    h = labconfig.get().history
    through = through or last_completed_day(conn)
    cal = calendar_days(conn, h.start, through)
    if not cal:
        return {"ok": False, "error": "no calendar stored; run history-download first"}
    through = through or cal[-1]
    cal_set = set(cal)
    out = {"through": through, "calendar_days": len(cal), "first_calendar_day": cal[0], "symbols": 0,
           "missing": {}, "stale": {}, "late_start": {}, "off_calendar": {}, "no_data": [], "failed": {}}
    for row in conn.execute("SELECT * FROM hist_symbols ORDER BY symbol").fetchall():
        s = row["symbol"]
        if row["status"] == "no_data":
            out["no_data"].append(s)
            continue
        if row["status"] == "failed":
            out["failed"][s] = row["error"]
            continue
        if row["status"] != "done":
            out["failed"][s] = f"status {row['status']}"
            continue
        out["symbols"] += 1
        days = {r[0] for r in conn.execute("SELECT day FROM hist_bars_daily WHERE symbol=? AND day<=?", (s, through))}
        if not days:
            continue
        first, last = min(days), max(days)
        expected = [d for d in cal if first <= d <= last]
        missing = [d for d in expected if d not in days]
        if missing:
            out["missing"][s] = missing[:max_listed] + ([f"... {len(missing)} in all"] if len(missing) > max_listed else [])
        off = sorted(days - cal_set)
        if off:
            out["off_calendar"][s] = off[:max_listed]
        if last < through:
            out["stale"][s] = last
        if first > cal[0]:
            out["late_start"][s] = first
    spy = conn.execute("SELECT MIN(day) FROM hist_bars_daily WHERE symbol='SPY'").fetchone()[0]
    out["spy_first_day"] = spy
    out["ok"] = spy == cal[0] and not out["missing"] and not out["off_calendar"] and not out["failed"]
    return out


# --------------------------------------------------------------------------
# Reading (the engine and the market-state labels)
# --------------------------------------------------------------------------

def bars(conn: sqlite3.Connection, symbols: Iterable[str] | None = None, start: str | None = None,
         end: str | None = None) -> pd.DataFrame:
    """Wide frame like the live research context's: MultiIndex columns (field, ticker), DatetimeIndex."""
    sql = "SELECT symbol AS ticker, day AS date, open, high, low, close, volume FROM hist_bars_daily WHERE day>=? AND day<=?"
    params: list = [start or "0000", end or "9999"]
    if symbols is not None:
        symbols = sorted(set(symbols))
        sql += f" AND symbol IN ({','.join('?' * len(symbols))})"
        params += symbols
    long = pd.read_sql_query(sql + " ORDER BY day, symbol", conn, params=params)
    if long.empty:
        return pd.DataFrame()
    long["date"] = pd.to_datetime(long["date"])
    return long.pivot(index="date", columns="ticker",
                      values=["open", "high", "low", "close", "volume"]).sort_index(axis=1)


def closes(conn: sqlite3.Connection, symbol: str, end: str | None = None) -> list[tuple[str, float]]:
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT day, close FROM hist_bars_daily WHERE symbol=? AND day<=? ORDER BY day", (symbol, end or "9999"))]
