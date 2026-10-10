"""The 08:00 Telegram status (daily on weekdays, weekly on Saturday)."""
from __future__ import annotations

from datetime import datetime

from arena import ledger, summary
from arena.prices import ET
from arena.scheduler import due_jobs

from .learning_helpers import league_week1, prices, set_price


def _league(conn):
    prices(conn)
    league_week1(conn, "claude", {1: ("steady", 0.5, {"KO": 0.2, "PG": 0.2})}, through="2024-01-09")
    league_week1(conn, "gpt", {1: ("growth", 1.0, {"AAPL": 0.2, "MSFT": 0.2})}, through="2024-01-09")
    for t in ("AAPL", "MSFT"):
        set_price(conn, t, "2024-01-10", 100, 110)
        for d in ("2024-01-11", "2024-01-12"):
            set_price(conn, t, d, 110)
    for a in ("claude", "gpt"):
        for d in ("2024-01-10", "2024-01-11", "2024-01-12"):
            ledger.snapshot(conn, a, d, "daily", "close")


def test_daily_status(conn):
    _league(conn)
    text = summary.daily(conn, "2024-01-10")
    assert "close of Wed Jan 10" in text
    gpt = text.split("GPT:")[1]
    assert gpt.strip().startswith("+$")            # +10% on 40% of the account
    assert "today" in text and "total" in text and "SPY" in text


def test_weekly_status_names_the_leader(conn):
    _league(conn)
    text = summary.weekly(conn, "2024-01-12")
    assert text.startswith("Weekly status")
    assert "Best this week: GPT" in text and "Leading overall: GPT" in text
    assert text.index("GPT:") < text.index("Claude:")    # best first


def test_scheduled_mon_to_sat_at_8():
    cal = {"2026-10-09": "16:00", "2026-10-12": "16:00"}
    at = lambda d, h: datetime.fromisoformat(d).replace(hour=h, tzinfo=ET)
    names = lambda now: [(j.name, j.date) for j in due_jobs(now, cal, {}, True)]
    assert ("morning-report", "2026-10-12") in names(at("2026-10-12", 8))      # Monday
    assert ("morning-report", "2026-10-10") in names(at("2026-10-10", 8))      # Saturday
    assert not any(n == "morning-report" for n, _ in names(at("2026-10-11", 8)))   # Sunday
    assert not any(n == "morning-report" for n, _ in names(at("2026-10-12", 7)))
    assert not any(n == "morning-report" for n, _ in names(at("2026-10-12", 13)))
