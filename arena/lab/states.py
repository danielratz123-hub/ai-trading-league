"""Market-state labels (SPY only) for every trading day, history and live.

One function, arena.learning.regime.classify_spy, labels every day from
SPY's closes in data/history.db up to and including that day. The nightly
history update appends the day's SPY bar and then labels it, so a live day
is labelled exactly like a day in 2017.

The first trend_sma_days - 1 trading days of the history (Jan-Oct 2016) have
no 200-day average yet and so no label; backtests start after them.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from ..learning.regime import classify_spy
from . import config as labconfig, histdata, schema


def label_all(conn: sqlite3.Connection, hconn: sqlite3.Connection, symbol: str = "SPY") -> dict:
    """(Re)label every day SPY has a bar for. Labels can change only if the
    adjusted history changed (a dividend rescales older closes)."""
    schema.migrate(conn)
    cfg = labconfig.get().market_state
    series = histdata.closes(hconn, symbol)
    feed = histdata.get_meta(hconn, "feed") or labconfig.get().history.feed
    now = datetime.now(timezone.utc).isoformat()
    closes = [c for _, c in series]
    rows = []
    for i, (day, _) in enumerate(series):
        label, inputs = classify_spy(closes[: i + 1], cfg)
        rows.append((day, label, json.dumps(inputs), f"history.db:{feed}", now))
    with conn:
        conn.executemany("INSERT OR REPLACE INTO market_states(day, label, inputs, source, computed_at) "
                         "VALUES (?,?,?,?,?)", rows)
    labelled = [r for r in rows if r[1]]
    counts: dict[str, int] = {}
    for r in labelled:
        counts[r[1]] = counts.get(r[1], 0) + 1
    return {"days": len(rows), "labelled": len(labelled),
            "first_label": labelled[0][0] if labelled else None,
            "last_label": labelled[-1][0] if labelled else None, "counts": counts}


def labels(conn: sqlite3.Connection, start: str | None = None, end: str | None = None) -> dict[str, str | None]:
    """{day: label} for start..end."""
    return {r[0]: r[1] for r in conn.execute(
        "SELECT day, label FROM market_states WHERE day >= ? AND day <= ? ORDER BY day",
        (start or "0000", end or "9999"))}


def coverage(conn: sqlite3.Connection, hconn: sqlite3.Connection, through: str) -> dict:
    """The phase-2 check: does every trading day have a label?

    history: every calendar day from the first labellable day (the
             trend_sma_days-th SPY bar) through `through`
    live:    every trading day since the league started (first week's
             execution day) through `through`
    """
    schema.migrate(conn)
    cal = histdata.calendar_days(hconn, labconfig.get().history.start, through)
    have = {d for d, lab in labels(conn).items() if lab}
    first = conn.execute("SELECT MIN(day) FROM market_states WHERE label IS NOT NULL").fetchone()[0]
    hist_days = [d for d in cal if first and d >= first]
    try:
        league_start = conn.execute("SELECT MIN(exec_date) FROM weeks").fetchone()[0]
    except sqlite3.OperationalError:
        league_start = None
    live_days = [d for d in cal if league_start and d >= league_start]
    miss_hist = [d for d in hist_days if d not in have]
    miss_live = [d for d in live_days if d not in have]
    return {"through": through, "first_labelled_day": first, "history_days": len(hist_days),
            "history_missing": miss_hist[:50], "league_start": league_start, "live_days": len(live_days),
            "live_missing": miss_live[:50],
            "unlabelled_warmup_days": len([d for d in cal if first and d < first]),
            "ok": bool(first) and not miss_hist and not miss_live}
