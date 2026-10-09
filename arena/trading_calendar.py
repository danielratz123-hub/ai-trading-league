"""Trading days and holidays, from Alpaca's calendar endpoint.

Named trading_calendar.py (not calendar.py) on purpose: a file called
calendar.py shadows Python's built-in `calendar` module whenever this folder
ends up on sys.path, which breaks pandas in very confusing ways.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from .config import require_env
from .db import transaction


def fetch_from_alpaca(start: str, end: str) -> list[tuple[str, str, str]]:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetCalendarRequest

    client = TradingClient(
        require_env("ALPACA_API_KEY"), require_env("ALPACA_SECRET_KEY"), paper=True
    )
    days = client.get_calendar(GetCalendarRequest(
        start=date.fromisoformat(start), end=date.fromisoformat(end)))
    return [(str(d.date), d.open.strftime("%H:%M"), d.close.strftime("%H:%M")) for d in days]


def store_days(conn: sqlite3.Connection, days: list[tuple[str, str, str]]) -> int:
    with transaction(conn):
        conn.executemany(
            "INSERT OR REPLACE INTO calendar(date, open_time, close_time) VALUES (?,?,?)",
            days,
        )
    return len(days)


def update_calendar(conn: sqlite3.Connection, start: str = "2015-01-01",
                    end: str | None = None) -> int:
    # Alpaca publishes the calendar well into the future, so one fetch per
    # year or so is plenty.
    end = end or (date.today() + timedelta(days=400)).isoformat()
    return store_days(conn, fetch_from_alpaca(start, end))


def _require_calendar(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM calendar LIMIT 1").fetchone() is None:
        raise RuntimeError("Trading calendar is empty. Run:  python -m arena.main update-calendar")


def is_trading_day(conn: sqlite3.Connection, d: str) -> bool:
    _require_calendar(conn)
    return conn.execute("SELECT 1 FROM calendar WHERE date=?", (d,)).fetchone() is not None


def next_trading_day(conn: sqlite3.Connection, d: str) -> str:
    """First trading day strictly after d."""
    _require_calendar(conn)
    row = conn.execute("SELECT MIN(date) FROM calendar WHERE date > ?", (d,)).fetchone()
    if row[0] is None:
        raise RuntimeError(f"No trading day after {d} in calendar; run update-calendar.")
    return row[0]


def previous_trading_day(conn: sqlite3.Connection, d: str) -> str:
    """Last trading day strictly before d."""
    _require_calendar(conn)
    row = conn.execute("SELECT MAX(date) FROM calendar WHERE date < ?", (d,)).fetchone()
    if row[0] is None:
        raise RuntimeError(f"No trading day before {d} in calendar.")
    return row[0]


def last_trading_day_on_or_before(conn: sqlite3.Connection, d: str) -> str:
    _require_calendar(conn)
    row = conn.execute("SELECT MAX(date) FROM calendar WHERE date <= ?", (d,)).fetchone()
    if row[0] is None:
        raise RuntimeError(f"No trading day on or before {d} in calendar.")
    return row[0]


def trading_days(conn: sqlite3.Connection, start: str, end: str) -> list[str]:
    _require_calendar(conn)
    rows = conn.execute(
        "SELECT date FROM calendar WHERE date BETWEEN ? AND ? ORDER BY date", (start, end)
    ).fetchall()
    return [r[0] for r in rows]


def calendar_days_between(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days
