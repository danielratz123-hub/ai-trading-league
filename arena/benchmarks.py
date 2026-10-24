"""Benchmark players, scored on the same weekly system as the agents.

A benchmark is NOT an agent: it has no row in `agents`, no ledger, no
snapshots, no orders. Its only footprint is one row per week in
`benchmark_scores`, so mirror.py, the leader pick and the kill switch never
see it, and it can never win the weekly point (score_week is untouched).

`bench_spy` holds 100% SPY and never trades. Its week is the agents' week:
SPY's open on exec_date to SPY's open on end_date (the mark), with the max
drawdown taken over that open, every daily close in between, and the end open.
Prices come from get_price at dates up to end_date only (no look-ahead).
"""
from __future__ import annotations

import sqlite3

from .db import log_event, transaction
from .learning import scores
from .prices import get_price
from .trading_calendar import trading_days

# One entry per benchmark player; add a second player by adding an id here and
# a path function to PATHS.
PLAYER_IDS = ["bench_spy"]


def _spy_path(conn: sqlite3.Connection, exec_date: str, end_date: str) -> list[float] | None:
    """SPY value path of $1 held over the week; None if an open is missing."""
    start = get_price(conn, "SPY", exec_date, "open")
    end = get_price(conn, "SPY", end_date, "open")
    if not start or not end:
        return None
    path = [start]
    for d in trading_days(conn, exec_date, end_date):
        if d >= end_date:
            break                                   # the end day's close is after the mark
        close = get_price(conn, "SPY", d, "close")
        if close:                                   # a missing bar is skipped
            path.append(close)
    path.append(end)
    return path


PATHS = {"bench_spy": _spy_path}


def score_benchmarks(conn: sqlite3.Connection, week_no: int, exec_date: str, end_date: str) -> dict[str, dict]:
    """Write benchmark_scores for week_no. Returns {player_id: row}; players
    whose prices are missing are skipped (logged), never raised."""
    out = {}
    mode = scores.mode_for(week_no)
    for pid in PLAYER_IDS:
        path = PATHS[pid](conn, exec_date, end_date)
        if path is None:
            log_event(conn, "warning", "benchmarks", f"week {week_no}: no prices for {pid}, not scored")
            continue
        ret_pct = (path[-1] / path[0] - 1) * 100
        dd = scores.max_drawdown_pct(path)
        sc = scores.score(ret_pct, dd, False) if mode == "risk_adjusted" else ret_pct
        row = {"return_pct": ret_pct, "max_drawdown_pct": dd, "score": sc, "mode": mode}
        conn.execute(
            "INSERT OR REPLACE INTO benchmark_scores(week_no, player_id, week_start, return_pct, "
            "max_drawdown_pct, score, mode) VALUES (?,?,?,?,?,?,?)",
            (week_no, pid, exec_date, round(ret_pct, 6), round(dd, 6), round(sc, 6), mode))
        out[pid] = row
    return out


def backfill_benchmarks(conn: sqlite3.Connection) -> int:
    """Score every already-scored week. Safe to run twice (same rows).
    Returns the number of weeks that got at least one row."""
    n = 0
    weeks = conn.execute("SELECT week_no, exec_date FROM weeks WHERE status='scored' ORDER BY week_no").fetchall()
    for w in weeks:
        end = conn.execute("SELECT MIN(date) FROM snapshots WHERE kind='mark' AND date>?",
                           (w["exec_date"],)).fetchone()[0]
        if end is None:
            continue
        try:
            with transaction(conn):
                rows = score_benchmarks(conn, w["week_no"], w["exec_date"], end)
        except Exception as e:  # noqa: BLE001 - one bad week must not stop the rest
            log_event(conn, "error", "benchmarks", f"backfill week {w['week_no']}: {type(e).__name__}: {e}")
            continue
        n += bool(rows)
    return n
