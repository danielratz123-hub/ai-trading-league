"""Benchmark players, scored on the same weekly system as the agents.

A benchmark is NOT an agent: it has no row in `agents`, no ledger, no
snapshots, no orders. Its only footprint is one row per week in
`benchmark_scores`, so mirror.py, the leader pick and the kill switch never
see it, and it can never win the weekly point (score_week is untouched).

`bench_spy` holds 100% SPY and never trades. Its week is the agents' week:
SPY's open on exec_date to SPY's open on end_date (the mark), with the max
drawdown taken over that open, every daily close in between, and the end open.
`bench_random` selects up to five tradable tickers using only decision-date
history through ResearchContext, seeded by week number. It holds equal initial
weights without costs. Missing endpoint prices are skipped; the original draw
is saved as JSON, including any skipped tickers. Both players share the same
portfolio valuation and scoring path.
"""
from __future__ import annotations

import json
import random
import sqlite3
from datetime import date, timedelta

from .db import log_event, transaction
from .learning import scores
from .prices import get_price
from .tools.context import ResearchContext
from .trading_calendar import trading_days

RANDOM_PICK_COUNT = 5
PLAYER_IDS = ["bench_spy", "bench_random"]


def _random_picks(conn: sqlite3.Connection, week_no: int, exec_date: str) -> list[str]:
    week = conn.execute("SELECT decision_date FROM weeks WHERE week_no=?", (week_no,)).fetchone()
    decision_date = week[0] if week else (date.fromisoformat(exec_date) - timedelta(days=1)).isoformat()
    ctx = ResearchContext(conn, "bench_random", decision_date)
    try:
        closes = ctx.field("close")
    except RuntimeError:  # no history available at the decision date
        return []
    eligible = sorted(t for t in ctx.universe if t in closes and closes[t].notna().any())
    return random.Random(f"bench_random:{week_no}").sample(eligible, min(RANDOM_PICK_COUNT, len(eligible)))


def _basket_path(conn: sqlite3.Connection, picks: list[str], exec_date: str,
                 end_date: str) -> list[float] | None:
    """Equal initial weights, fixed shares, no costs; omit missing endpoints.

    Missing intermediate closes carry forward the last known value, keeping
    portfolio weights intact when computing daily portfolio drawdown.
    """
    holdings = []
    for ticker in picks:
        start = get_price(conn, ticker, exec_date, "open")
        end = get_price(conn, ticker, end_date, "open")
        if start and end:
            holdings.append((ticker, start, end))
    if not holdings:
        return None
    last = {t: start for t, start, _ in holdings}
    path = [1.0]
    for d in trading_days(conn, exec_date, end_date):
        if d >= end_date:
            break
        for t, _, _ in holdings:
            close = get_price(conn, t, d, "close")
            if close:
                last[t] = close
        path.append(sum(last[t] / start for t, start, _ in holdings) / len(holdings))
    path.append(sum(end / start for _, start, end in holdings) / len(holdings))
    return path


def score_benchmarks(conn: sqlite3.Connection, week_no: int, exec_date: str, end_date: str) -> dict[str, dict]:
    """Write benchmark_scores for week_no. Returns {player_id: row}; players
    whose prices are missing are skipped (logged), never raised."""
    out = {}
    mode = scores.mode_for(week_no)
    for pid in PLAYER_IDS:
        picks = _random_picks(conn, week_no, exec_date) if pid == "bench_random" else ["SPY"]
        path = _basket_path(conn, picks, exec_date, end_date)
        if path is None:
            log_event(conn, "warning", "benchmarks", f"week {week_no}: no prices for {pid}, not scored")
            continue
        ret_pct = (path[-1] / path[0] - 1) * 100
        dd = scores.max_drawdown_pct(path)
        sc = scores.score(ret_pct, dd, False) if mode == "risk_adjusted" else ret_pct
        row = {"return_pct": ret_pct, "max_drawdown_pct": dd, "score": sc, "mode": mode}
        conn.execute(
            "INSERT OR REPLACE INTO benchmark_scores(week_no, player_id, week_start, return_pct, "
            "max_drawdown_pct, score, mode, picks) VALUES (?,?,?,?,?,?,?,?)",
            (week_no, pid, exec_date, round(ret_pct, 6), round(dd, 6), round(sc, 6), mode,
             json.dumps(picks) if pid == "bench_random" else None))
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
