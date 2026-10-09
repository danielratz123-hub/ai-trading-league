"""Shared test fixtures: in-memory DB, a fake calendar, fake prices. No network."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from arena import ledger
from arena.db import connect
from arena.trading_calendar import store_days

HOLIDAYS = {"2024-01-15", "2024-02-19"}  # MLK day, Presidents' day


def weekdays(start: str, end: str) -> list[str]:
    d, out = date.fromisoformat(start), []
    while d <= date.fromisoformat(end):
        if d.weekday() < 5 and d.isoformat() not in HOLIDAYS:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


DAYS = weekdays("2024-01-02", "2024-03-29")


@pytest.fixture
def conn():
    c = connect(":memory:")
    store_days(c, [(d, "09:30", "16:00") for d in DAYS])
    yield c
    c.close()


def set_price(conn, ticker: str, d: str, open_: float, close: float | None = None) -> None:
    close = open_ if close is None else close
    conn.execute(
        "INSERT OR REPLACE INTO prices(ticker, date, open, high, low, close, volume) "
        "VALUES (?,?,?,?,?,?,?)",
        (ticker, d, open_, max(open_, close), min(open_, close), close, 1e6))


def flat_prices(conn, ticker: str, price: float, days=DAYS) -> None:
    for d in days:
        set_price(conn, ticker, d, price)


@pytest.fixture
def agent(conn):
    """One agent with $1,000, created on the first trading day."""
    ledger.create_agent(conn, "claude", 1000.0, DAYS[0])
    return "claude"
