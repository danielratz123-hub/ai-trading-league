"""Staging (ROADMAP 0.6): replay a full trading day on a COPY of the league.

    python -m arena.main staging [--date D] [--with-week] [--fake-agents]

  * copies the live database to data/staging.db (fresh every run); the live
    file is only read
  * the paper mirror is forced off and prices come from the database, so
    nothing reaches Alpaca
  * replays day D with the learning layer on: the open (pending orders,
    tilts), every hourly risk check (on 1-minute prices interpolated from D's
    open, low, high and close), the close (daily), the daily review, and with
    --with-week the weekly review after D
  * real agents unless --fake-agents (then no model is called)

The weekly decide/execute loop has its own replay: `simulate` (past weeks,
separate simulation.db).
"""
from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import league, ledger
from .db import connect, transaction
from .prices import ET, get_price
from .trading_calendar import is_trading_day, last_trading_day_on_or_before


def copy_db(src: str | Path, dst: str | Path) -> None:
    dst = Path(dst)
    for suffix in ("", "-wal", "-shm"):
        Path(str(dst) + suffix).unlink(missing_ok=True)
    s, d = sqlite3.connect(f"file:{Path(src).resolve().as_posix()}?mode=ro", uri=True), sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        s.close()
        d.close()


def synth_minutes(conn: sqlite3.Connection, d: str, symbols: list[str]) -> int:
    """1-minute bars for day d from its daily bar: open at 09:30, low at
    11:30, high at 13:30, close at 15:59 (straight lines in between). Enough
    for the risk rules to see the day's swings."""
    rows = []
    marks = [(9, 30, "open"), (11, 30, "low"), (13, 30, "high"), (15, 59, "close")]
    for sym in symbols:
        pts = []
        for h, m, f in marks:
            p = get_price(conn, sym, d, f)
            if p is None:
                break
            pts.append((datetime.fromisoformat(d).replace(hour=h, minute=m, tzinfo=ET), p))
        if len(pts) != len(marks):
            continue
        for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
            n = int((t1 - t0).total_seconds() // 60)
            for i in range(n):
                t = t0 + timedelta(minutes=i)
                px = p0 + (p1 - p0) * i / n
                rows.append((sym, t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z"), px, px, px, px, 0, px))
    with transaction(conn):
        conn.executemany("INSERT OR REPLACE INTO bars_1m(symbol, ts, open, high, low, close, volume, vwap) "
                         "VALUES (?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def run_day(live_db: str | Path, staging_db: str | Path, d: str | None, runner, week: bool = False,
            log=print) -> dict:
    os.environ["MIRROR_MODE"] = "off"          # this process only: never the paper account
    os.environ["LEARNING_ENABLED"] = "1"
    copy_db(live_db, staging_db)
    conn = connect(staging_db)
    try:
        from .learning import collector, config as lconfig, jobs
        d = d or conn.execute("SELECT MAX(date) FROM prices WHERE ticker='SPY'").fetchone()[0]
        if d is None or not is_trading_day(conn, d):
            raise league.LeagueError(f"{d} is not a trading day with prices in the database.")
        out: dict = {"day": d, "staging_db": str(staging_db)}
        log(f"== Staging copy: {staging_db} (the live league is untouched; mirror off)")
        out["bootstrap"] = jobs.ensure_bootstrap(conn)
        log(f"== Open of {d}")
        try:
            rep = league.run_open(conn, d)
            out["open"] = {"weekly": bool(rep.weekly), "risk_fills": rep.risk_fills}
        except league.LeagueError as e:
            out["open"] = f"skipped: {e}"
        n = synth_minutes(conn, d, collector.watched_symbols(conn))
        log(f"== {n} synthetic 1-minute bars; risk checks")
        close = conn.execute("SELECT close_time FROM calendar WHERE date=?", (d,)).fetchone()[0]
        times = lconfig.get().risk_check_times(tuple(map(int, close.split(":"))))
        out["risk_checks"] = []
        for h, m in times:
            now = datetime.fromisoformat(d).replace(hour=h, minute=m, tzinfo=ET)
            r = jobs.risk_check(conn, d, runner, dry_run=True, now=now)
            out["risk_checks"].append({"time": f"{h:02d}:{m:02d}", "fired": r.get("fired"),
                                       "woken": [w.get("agent") for w in r.get("woken", [])]})
            log(f"   {h:02d}:{m:02d} ET: {r.get('fired', 0)} rule(s) fired, woke {[w.get('agent') for w in r.get('woken', [])]}")
        log("== Close")
        out["daily"] = {a: ("already processed" if rep.skipped else f"{len(rep.new_orders)} new orders")
                        for a, rep in league.daily(conn, d).items()}
        log("== Daily review")
        out["daily_review"] = jobs.daily_review(conn, d, runner, dry_run=True, vix_fetch=None, news_fetch=None)
        if week:
            sat = date.fromisoformat(d)
            while sat.weekday() != 5:
                sat += timedelta(days=1)
            log(f"== Weekly review ({sat})")
            out["weekly_review"] = jobs.weekly_review(conn, sat.isoformat(), runner, dry_run=True)
        out["equity"] = {a: round(ledger.value_agent(conn, a, last_trading_day_on_or_before(conn, d), "close").equity, 2)
                         for a in ledger.agent_ids(conn)}
        return out
    finally:
        conn.close()
