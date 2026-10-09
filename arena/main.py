"""Command-line entry point.

Run from the project folder:
    python -m arena.main <command> [options]

Setup
    init-db          create the database file and tables
    update-calendar  download trading days / holidays from Alpaca
    update-prices    download or update adjusted daily bars
    check-agents     check that Claude Code and Codex are installed and signed in
                     (add --probe to run a tiny test turn through each)
    check-logins     the scheduler's login health check: a tiny probe turn per agent,
                     Telegram alert if one fails (exit 1)
League (times are US Eastern)
    kickoff          create both agents with starting cash and run week 1's decisions
    decide           weekend: update prices, run both agents' turns, store pending orders
    execute          every trading day ~9:50: fetch opening prices, fill pending stop /
                     take-profit / forced-cover orders at the open, and on execution days
                     score last week and rebalance at the open
    daily            every trading day after ~16:30: update prices, borrow fees, stop and
                     forced-cover checks, daily snapshot
    status           scoreboard, equity, positions
    simulate         replay past weeks with real agent turns (separate database)
    replan           redo the first week's decisions (only before any trade) with the latest data
    trade-now        during market hours: fill the waiting week now at the latest price
    complete-now     during market hours: buy planned positions that had no price, fix negative cash
    pause / resume   stop / restart decide and execute
    app              the easy way: dashboard + setup page, opens your browser (start.cmd runs this)
    serve            run the dashboard + read-only API on http://127.0.0.1:8000
    hash-password    make DASHBOARD_PASSWORD_HASH for the dashboard login (asks for the password)
    scheduler        run decide / execute / daily automatically on US market time
    health           quick check (database, disk; --scheduler: heartbeat too); exit 1 if unhealthy
    reports          write the weekly strategy reports for scored weeks that have none
                     (the scheduler does this after execute; --week N --force to redo one)
    refresh-universe download the current S&P 500 list (--source URL/CSV file; --date list date)
    backup           back up the database now (online backup API, compressed, uploaded if configured)
    backups          list the backups (local and remote)
    restore-backup   NAME|latest --to PATH: download, check and unpack a backup (used by scripts/restore.sh)
    test-alert       send a test Telegram alert
    mirror           show (or with --apply, place) the paper-account orders that copy the leader
Tools
    tool             try a research tool by hand, e.g.
                     python -m arena.main tool screen --args "{\"conditions\": [\"rsi_14 < 30\"]}"

--dry-run runs a command against a throwaway copy of the database, so
nothing is saved. (Agent turns still use your Claude/ChatGPT plan's usage.)
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from . import league, ledger, prices, scoring, trading_calendar
from .agents.cli import CLI_ADAPTERS
from .config import SETTINGS, MissingKeyError
from .db import connect, dry_run_guard, log_event, transaction
from .league import LeagueError
from .prices import ET
from .universe import ALL_TICKERS


def cmd_init_db(conn, args) -> None:
    print(f"Database ready at {args.db}")


def cmd_update_calendar(conn, args) -> None:
    n = trading_calendar.update_calendar(conn, start=SETTINGS.price_history_start)
    print(f"Stored {n} trading days.")


def _update_prices(conn, tickers) -> None:
    # Held tickers too: one that has left the S&P 500 still needs prices.
    held = [r[0] for r in conn.execute("SELECT DISTINCT ticker FROM positions")]
    tickers = sorted(set(tickers) | set(held))
    source = prices.AlpacaPriceSource()
    print(f"Updating {len(tickers)} tickers (first run downloads history since "
          f"{SETTINGS.price_history_start}; this can take a few minutes)...")
    rep = prices.update_prices(conn, source, tickers, on_adjust=ledger.apply_price_adjustment)
    print(f"Feed used: {source.feed}. Rows written: {rep.appended}.")
    if rep.redownloaded:
        print("Re-downloaded after split/dividend: " +
              ", ".join(f"{t} (x{rep.adjustments[t]:.4f})" for t in rep.redownloaded))
    if rep.missing:
        print(f"No data for {len(rep.missing)} tickers: {', '.join(rep.missing)}")


def cmd_update_prices(conn, args) -> None:
    _update_prices(conn, args.tickers.split(",") if args.tickers else ALL_TICKERS)


def _print_turns(reports) -> None:
    for r in reports:
        line = f"  {r.agent_id}: {r.status}  tools {r.tool_calls}  web {r.web_searches}  submits {r.attempts}"
        if SETTINGS.agent_backend == "api":
            line += f"  cost ${r.cost_usd:.3f}"
        print(line + (f"\n    error: {r.error}" if r.error else ""))


def _usage_note() -> None:
    if SETTINGS.agent_backend == "cli":
        print("Running both agents through Claude Code and Codex on your subscriptions "
              "(this uses your plans' usage limits; a turn can take several minutes).")
    else:
        print(f"Note: this calls the paid APIs. Worst case about ${league.estimated_cost(1):.2f} per week.")


def cmd_kickoff(conn, args) -> None:
    if ledger.agent_ids(conn):
        sys.exit("Agents already exist; the league has started. Use `decide` for later weeks.")
    _usage_note()
    d = args.date
    if d is None:
        latest = prices.latest_price_date(conn)
        if latest is None:
            sys.exit("No prices yet. Download market data first (update-calendar, update-prices).")
        # Mid-week, start from last week's close; the first trades then
        # happen at the next morning's open.
        d = league.week_end_on_or_before(conn, latest)
        if d != latest:
            print(f"Starting from the last full week (data up to {d}); first trades at the next open.")
    reports = league.decide(conn, d)
    print(f"Kickoff: agents created with ${SETTINGS.starting_cash:,.2f} each.")
    _print_turns(reports)


def cmd_decide(conn, args) -> None:
    if not args.no_update:
        _update_prices(conn, ALL_TICKERS)
    _usage_note()
    _print_turns(league.decide(conn, args.date))


def cmd_execute(conn, args) -> None:
    now = datetime.now(ET)
    today = now.date().isoformat()
    d = args.date or today
    if not trading_calendar.is_trading_day(conn, d):
        print(f"{d} is not a trading day; nothing to do.")
        return
    if d == today and (now.hour, now.minute) < (9, 46):
        sys.exit("Run execute at 9:46 ET or later: Alpaca's free data plan serves market data with "
                 "a 15-minute delay, so the opening prices aren't available before then.")
    close = conn.execute("SELECT close_time FROM calendar WHERE date=?", (d,)).fetchone()
    if d < today or (d == today and close and now.strftime("%H:%M") >= close[0]):
        sys.exit(f"The market has already closed for {d}, so its opening price is history. "
                 f"Orders wait for the next open; the scheduler runs execute at 9:50 ET.")
    if not args.no_update:
        source = prices.AlpacaPriceSource()
        missing = league.prepare_open(conn, d, source)
        print(f"Opening prices for {d} fetched (feed {source.feed})"
              + (f"; missing {missing}" if missing else ""))
    rep = league.run_open(conn, d)
    for a, n in rep.risk_fills.items():
        if n:
            print(f"{a}: {n} stop / take-profit / forced-cover order(s) filled at the open")
    w = rep.weekly
    if w is None:
        print("No weekly orders due today.")
        _maybe_mirror(conn, d, args)
        _execute_summary(conn, d, rep, args)
        return
    _maybe_mirror(conn, d, args)
    _execute_summary(conn, d, rep, args)
    print(f"Executed week {w.week_no} at the {w.exec_date} open. Trades: {w.trades}")
    if w.scored_week:
        print(f"Scored week {w.scored_week}: " + ", ".join(f"{a} {r:+.2%}" for a, r in w.weekly_returns.items()))
    if w.bonus_winner:
        print(f"Bonus ${SETTINGS.bonus_amount:,.0f} deposited to {w.bonus_winner}.")


def cmd_daily(conn, args) -> None:
    if not args.no_update:
        _update_prices(conn, ALL_TICKERS)
    d = args.date or prices.latest_price_date(conn)
    if d is None:
        sys.exit("No prices in the database yet. Run update-prices first.")
    if not trading_calendar.is_trading_day(conn, d):
        print(f"{d} is not a trading day; nothing to do.")
        return
    for a, rep in league.daily(conn, d).items():
        if rep.skipped:
            print(f"{a}: {d} already processed.")
            continue
        print(f"{a}: {len(rep.executed)} late risk fills, borrow ${rep.borrow_fees:.4f}, "
              f"{len(rep.new_orders)} new orders for the next open")
        for o in rep.new_orders:
            print(f"   {o}")


def cmd_replan(conn, args) -> None:
    """Redo the league's first (never-traded) week with data up to the latest close."""
    removed = league.reset_unexecuted_start(conn)
    if not args.no_update:
        _update_prices(conn, ALL_TICKERS)
    d = args.date or prices.latest_price_date(conn)
    print(f"Removed {removed} untraded week(s). Both agents now decide with data up to {d}.")
    _usage_note()
    _print_turns(league.decide(conn, d))


def cmd_trade_now(conn, args) -> None:
    """Fill the waiting week right away at the latest market price."""
    now = datetime.now(ET)
    d = now.date().isoformat()
    close = conn.execute("SELECT open_time, close_time FROM calendar WHERE date=?", (d,)).fetchone()
    if close is None or not (close[0] <= now.strftime("%H:%M") < close[1]):
        sys.exit("The market isn't open right now; use execute at the next open instead.")
    source = prices.AlpacaPriceSource()
    rep = league.execute_now(conn, d, source)
    src = conn.execute("SELECT source FROM opening_prices WHERE date=? LIMIT 1", (d,)).fetchone()[0]
    print(f"Week {rep.week_no} traded now at market prices ({src}). Trades: {rep.trades}")
    _maybe_mirror(conn, d, args)


def cmd_complete_now(conn, args) -> None:
    """Buy planned positions that couldn't be filled, and fix slots below $0 cash, at the latest price."""
    now = datetime.now(ET)
    d = now.date().isoformat()
    hours = conn.execute("SELECT open_time, close_time FROM calendar WHERE date=?", (d,)).fetchone()
    if hours is None or not (hours[0] <= now.strftime("%H:%M") < hours[1]):
        sys.exit("The market isn't open right now.")
    done = league.complete_week_now(conn, d, prices.AlpacaPriceSource())
    print("\n".join(done) if done else "Nothing to complete: every planned position is filled and no cash is negative.")
    if done:
        _maybe_mirror(conn, d, args)


def cmd_status(conn, args) -> None:
    d = prices.latest_price_date(conn)
    agents = ledger.agent_ids(conn)
    print(f"Prices up to: {d or '(none)'}" + ("   [LEAGUE PAUSED]" if league.is_paused(conn) else ""))
    if not agents:
        print("No agents yet. Run `kickoff` to start the league.")
        return
    week = conn.execute("SELECT * FROM weeks ORDER BY week_no DESC LIMIT 1").fetchone()
    if week:
        print(f"Latest week: {week['week_no']} (decided {week['decision_date']}, exec "
              f"{week['exec_date']}, {week['status']})")
    print(f"\n{'agent':<8}{'points':>7}{'equity':>13}{'cumulative':>12}{'this week':>11}")
    for r in scoring.scoreboard(conn, d):
        wtd = f"{r['week_to_date']:+.2%}" if r["week_to_date"] is not None else "n/a"
        print(f"{r['agent']:<8}{r['points']:>7}{r['equity']:>13,.2f}{r['cumulative_return']:>+12.2%}{wtd:>11}")
    for a in agents:
        v = ledger.value_agent(conn, a, d, "close")
        print(f"\n== {a} == equity ${v.equity:,.2f}  cash ${v.cash:,.2f}  long ${v.long_value:,.2f}  "
              f"short ${v.short_value:,.2f}  gross {v.gross / v.equity if v.equity else 0:.1%}")
        for p in ledger.positions(conn, a):
            px = v.prices[p["ticker"]]
            pnl = (px - p["avg_price"]) * p["shares"]
            print(f"   slot {p['slot']}  {p['ticker']:<6} {p['shares']:>12.4f} sh  "
                  f"avg {p['avg_price']:>9.2f}  last {px:>9.2f}  P&L {pnl:>+9.2f}")


def cmd_simulate(conn, args) -> None:
    if not args.start or not args.weeks:
        sys.exit("Usage: simulate --start YYYY-MM-DD --weeks N")
    _usage_note()
    print("Simulate tests the loop end-to-end. Results are not a fair test of skill: the models may "
          "remember what happened in the past from their training data.")
    if args.weeks > 4 and not args.yes:
        answer = input(f"Run {args.weeks} weeks (> 4)? Type 'yes' to continue: ")
        if answer.strip().lower() != "yes":
            sys.exit("Cancelled.")
    sim_path = Path(args.sim_db)
    sim = league.new_simulation_db(conn, sim_path)
    print(f"Simulation database: {sim_path} (the live league is untouched)")
    try:
        league.simulate(sim, args.start, args.weeks, reports=args.reports)
        print()
        cmd_status(sim, args)
    finally:
        sim.close()


def cmd_check_agents(conn, args) -> None:
    from .agents import cli

    for agent_id, cls in CLI_ADAPTERS.items():
        agent = cls(agent_id, SETTINGS.models[agent_id])
        print(f"\n== {agent_id}: {cls.__name__}, model {agent.model or '(CLI default)'}")
        ok, text = agent.check_install()
        print(("  OK  " if ok else "  FAIL") + f" installed: {text}")
        if not ok:
            print(f"       {agent.INSTALL_HINT}")
            continue
        ok, text = agent.check_login()
        print(("  OK  " if ok else "  FAIL") + f" signed in: {text}")
        if not ok:
            print(f"       {agent.LOGIN_HINT}")
            continue
        if args.probe:
            res = cli.probe(agent)
            print(("  OK  " if res["ok"] else "  FAIL") + f" probe turn: {res['summary']}")


def cmd_check_logins(conn, args) -> None:
    """Login health check (daily, and 2 hours before every decide). Runs the
    same probe turn as `check-agents --probe` for each agent and alerts if
    one can't do a real turn. Uses a little plan usage."""
    from . import alerts
    from .agents import cli

    problems = {}
    for agent_id, cls in CLI_ADAPTERS.items():
        agent = cls(agent_id, SETTINGS.models[agent_id])
        ok, text = agent.check_install()
        if ok:
            ok, text = agent.check_login()
        if ok:
            try:
                res = cli.probe(agent)
                ok, text = res["ok"], res["summary"]
            except Exception as e:  # noqa: BLE001 - report, don't crash
                ok, text = False, f"probe crashed: {type(e).__name__}: {e}"
        print(("OK   " if ok else "FAIL ") + f"{agent_id}: {text[:300]}")
        if not ok:
            problems[agent_id] = text[:300]
            with transaction(conn):
                log_event(conn, "error", f"login:{agent_id}", f"login check failed: {text[:300]}")
    if problems:
        why = f" ({args.reason})" if getattr(args, "reason", None) else ""
        alerts.send("LOGIN CHECK FAILED" + why + ":\n"
                    + "\n".join(f"- {a}: {t}" for a, t in problems.items())
                    + "\nFix it before the next decide (DEPLOY.md, 'Sign the agents in').",
                    level="error", key="login-check-" + ",".join(sorted(problems)))
        sys.exit(1)


def cmd_pause(conn, args) -> None:
    league.set_paused(conn, True)
    print("League paused: decide and execute will refuse to run. (daily keeps running so open "
          "positions stay protected.)")


def cmd_resume(conn, args) -> None:
    league.set_paused(conn, False)
    print("League resumed.")


def _maybe_mirror(conn, d, args) -> None:
    from . import alerts, mirror
    if mirror.mode() == "off":
        return
    if args.dry_run:
        print("Paper mirror skipped in --dry-run (it would place real paper orders).")
        return
    problems = []
    try:
        for agent, lines in mirror.sync_all(conn, d).items():
            n = sum(len(l.orders) for l in lines)
            notes = "; ".join(f"{l.ticker}: {l.note}" for l in lines if l.note)
            print(f"Paper mirror ({agent}): {n} order(s) sent" + (f"; notes: {notes}" if notes else ""))
            problems += [f"{agent} {l.ticker}: {l.note}" for l in lines if "rejected" in (l.note or "")]
    except Exception as e:  # noqa: BLE001 - the league itself already executed fine
        print(f"Paper mirror failed (league unaffected): {e}")
        problems.append(f"sync failed: {type(e).__name__}: {str(e)[:300]}")
    if problems:
        alerts.send("Alpaca paper mirror problem (the league itself is fine):\n"
                    + "\n".join(problems[:10])[:1500], level="warning")


def _execute_summary(conn, d, rep, args) -> None:
    """Telegram after every execute: trades, equity per agent, weekly winner."""
    if args.dry_run:
        return
    from . import alerts
    try:
        lines = [f"Execute {d} done."]
        w = rep.weekly
        if w is not None:
            lines.append(f"Week {w.week_no} rebalanced at the open.")
        for a in ledger.agent_ids(conn):
            t = conn.execute("SELECT COUNT(*), COALESCE(SUM(value), 0) FROM trades WHERE agent_id=? AND date=?",
                             (a, d)).fetchone()
            v = ledger.value_agent(conn, a, d, "open")
            lines.append(f"{a}: {t[0]} trade(s) (${t[1]:,.0f}); equity ${v.equity:,.2f}")
        if w is not None and w.scored_week and w.weekly_returns:
            best = max(w.weekly_returns.values())
            winners = [a for a, r in w.weekly_returns.items() if r == best]
            returns = ", ".join(f"{a} {r:+.2%}" for a, r in w.weekly_returns.items())
            lines.append(f"Week {w.scored_week} scored: {returns}. "
                         + (f"Winner: {winners[0]}." if len(winners) == 1 else "A tie."))
        if w is not None and w.bonus_winner:
            lines.append(f"Bonus ${SETTINGS.bonus_amount:,.0f} deposited to {w.bonus_winner}.")
        alerts.send("\n".join(lines), level="info")
    except Exception as e:  # noqa: BLE001 - a summary must never fail the execute job
        print(f"(could not send the execute summary: {type(e).__name__})")


def _mirror_date(conn) -> str:
    """Mirror at the latest prices the league traded at (today's, if it traded today)."""
    today = datetime.now(ET).date().isoformat()
    if conn.execute("SELECT 1 FROM opening_prices WHERE date=? LIMIT 1", (today,)).fetchone():
        return today
    return prices.latest_price_date(conn)


def cmd_mirror(conn, args) -> None:
    from . import mirror
    m = mirror.mode()
    if m == "off":
        sys.exit("The paper mirror is off. Turn it on in Setup (step 6) or set MIRROR_MODE in .env.")
    d = args.date or _mirror_date(conn)
    results = mirror.sync_all(conn, d, dry_run=not args.apply)
    if not results:
        print("Nothing to mirror (no single leader yet)." if m == "leader" else "No agents yet.")
    for agent, lines in results.items():
        print(f"== {agent}'s share of the shared paper account" if m == "combined" else f"== {agent}'s paper account")
        for l in lines:
            print(f"  {l.ticker:<6} weight {l.virtual_weight:+.2%}  now {l.current_qty:g}  target {l.target_qty:g}  "
                  f"orders {[round(q, 4) for q in l.orders]}  {l.note}")
    print("(orders placed in the paper account(s))" if args.apply else "(plan only; add --apply to place paper orders)")


def cmd_scheduler(conn, args) -> None:
    from . import scheduler
    logging.getLogger().setLevel(logging.INFO)
    print("Scheduler running (Ctrl+C to stop). Job output goes to data/logs/.")
    try:
        scheduler.run_forever(args.db)
    except scheduler.AlreadyRunning as e:
        sys.exit(str(e))


def cmd_app(conn, args) -> None:
    import os
    import threading
    import webbrowser

    import uvicorn

    from .envfile import ensure_env_file

    ensure_env_file()
    os.environ["ARENA_DB"] = str(Path(args.db).resolve())
    url = f"http://127.0.0.1:{args.port}/"
    has_league = bool(ledger.agent_ids(conn))
    page = url if has_league else url + "setup.html"
    print(f"AI Trading League is running at {url}")
    print("Keep this window open while the league runs. Close it (or press Ctrl+C) to stop.")
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(page)).start()
    # 127.0.0.1 only: the setup page must never be reachable from the network.
    uvicorn.run("arena.api.app:app", host="127.0.0.1", port=args.port, log_level="warning")


def cmd_serve(conn, args) -> None:
    import os

    import uvicorn

    os.environ["ARENA_DB"] = str(Path(args.db).resolve())
    from .api.auth import ConfigError, check_config
    try:
        check_config()
    except ConfigError as e:
        sys.exit(str(e))
    print(f"Dashboard: http://{args.host}:{args.port}   (API docs at /api/docs)")
    uvicorn.run("arena.api.app:app", host=args.host, port=args.port, log_level="warning")


def cmd_hash_password(conn, args) -> None:
    import getpass

    from .api.auth import hash_password
    pw = getpass.getpass("Dashboard password (at least 12 characters): ")
    if len(pw) < 12:
        sys.exit("Too short: use at least 12 characters.")
    if getpass.getpass("Type it again: ") != pw:
        sys.exit("The two passwords don't match.")
    h = hash_password(pw)
    print("\nPut this line in .env (the $$ are needed by Docker Compose):\n")
    print("DASHBOARD_PASSWORD_HASH=" + h.replace("$", "$$"))


def _storage():
    from .backup import remote_storage
    return remote_storage()


def cmd_reports(conn, args) -> None:
    from . import reports
    from .agents import make_agent
    if not SETTINGS.reports_enabled:
        print("Strategy reports are switched off (REPORTS=0).")
        return
    done = reports.run_reports(conn, make_agent, weeks=[args.week] if args.week else None, force=args.force)
    if not done:
        print("No reports to write: every scored week has its reports.")
    for r in done:
        print(f"week {r['week']} {r['agent']}: {r['status']}" + (f" - {r['error']}" if r.get("error") else ""))


def cmd_refresh_universe(conn, args) -> None:
    from . import universe
    source = args.source or universe.DEFAULT_SOURCE
    rows, list_date = universe.fetch(source)
    today = datetime.now(ET).date().isoformat()
    diff = universe.refresh(conn, rows, today, source, args.date or list_date)
    info = universe.list_info(conn)
    print(f"S&P 500 list of {info['list_date']} from {source}: {info['members']} members.")
    print(f"Added ({len(diff['added'])}): {', '.join(diff['added']) or '-'}")
    print(f"Removed ({len(diff['removed'])}): {', '.join(diff['removed']) or '-'}"
          + ("  (held positions in them can be kept or reduced, not increased)" if diff["removed"] else ""))
    if diff["back"]:
        print(f"Back in the index: {', '.join(diff['back'])}")
    if diff["added"] and not args.no_update:
        print("Downloading price history for the new tickers...")
        _update_prices(conn, diff["added"])


def cmd_backup(conn, args) -> None:
    from . import alerts
    from .backup import run_backup
    try:
        res = run_backup(args.db, _storage())
    except Exception as e:  # noqa: BLE001 - report clearly; the scheduler retries
        alerts.send(f"Backup FAILED: {type(e).__name__}: {str(e)[:300]}", level="error")
        sys.exit(f"Backup failed: {type(e).__name__}: {e}")
    print(f"Backup {res.local.name}: {res.size / 1e6:.2f} MB")
    print("Uploaded: " + (", ".join(res.uploaded) if res.uploaded else "nothing (BACKUP_S3_BUCKET not set; local copy only)"))
    if res.deleted:
        print("Removed old copies: " + ", ".join(res.deleted))


def cmd_backups(conn, args) -> None:
    from .backup import list_backups
    for name in list_backups(args.db, _storage()):
        print(name)


def cmd_restore_backup(conn, args) -> None:
    from .backup import fetch_backup
    if not args.to:
        sys.exit("Usage: restore-backup NAME|latest --to PATH   (scripts/restore.sh does the rest)")
    if Path(args.to).resolve() == Path(args.db).resolve():
        sys.exit("--to must not be the live database; scripts/restore.sh swaps the files safely.")
    name = fetch_backup(args.name or "latest", args.to, args.db, _storage())
    print(f"Restored {name} to {args.to} (integrity check passed)")


def cmd_test_alert(conn, args) -> None:
    from . import alerts
    if not alerts.configured():
        sys.exit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set in .env.")
    ok = alerts.send("Test alert: if you can read this, alerts work.", level="good")
    print("Sent." if ok else "Telegram did not accept the message (check the token and chat id; see the log).")
    if not ok:
        sys.exit(1)


def cmd_tool(conn, args) -> None:
    from .tools.context import ResearchContext
    from .tools.registry import ToolBox

    if not args.name:
        sys.exit("Usage: tool <name> [--args JSON] [--date YYYY-MM-DD]")
    d = args.date or prices.latest_price_date(conn)
    if d is None:
        sys.exit("No prices in the database yet. Run update-prices first.")
    ctx = ResearchContext(conn, "manual", d)
    try:
        with dry_run_guard(conn, dry_run=True):   # never keep manual tool calls
            print(ToolBox(ctx).call(args.name, json.loads(args.args or "{}")))
    finally:
        ctx.close_session()


COMMANDS = {
    "init-db": cmd_init_db,
    "update-calendar": cmd_update_calendar,
    "update-prices": cmd_update_prices,
    "check-agents": cmd_check_agents,
    "check-logins": cmd_check_logins,
    "kickoff": cmd_kickoff,
    "decide": cmd_decide,
    "execute": cmd_execute,
    "daily": cmd_daily,
    "status": cmd_status,
    "replan": cmd_replan,
    "trade-now": cmd_trade_now,
    "complete-now": cmd_complete_now,
    "simulate": cmd_simulate,
    "pause": cmd_pause,
    "resume": cmd_resume,
    "app": cmd_app,
    "serve": cmd_serve,
    "scheduler": cmd_scheduler,
    "mirror": cmd_mirror,
    "tool": cmd_tool,
    "health": None,
    "hash-password": cmd_hash_password,
    "backup": cmd_backup,
    "refresh-universe": cmd_refresh_universe,
    "reports": cmd_reports,
    "backups": cmd_backups,
    "restore-backup": None,  # handled in main() before the database is opened
    "test-alert": cmd_test_alert,          # handled in main() before the database is opened
}
READ_ONLY = {"status", "tool", "check-agents", "init-db", "serve", "scheduler", "app", "hash-password",
             "backup", "backups", "restore-backup", "test-alert", "check-logins", "health"}


def _throwaway_copy(db: str, workdir: str) -> str:
    """Copy the database for --dry-run. A copy (rather than a rolled-back
    transaction) is needed because CLI agents' tool servers write to the
    database from their own processes."""
    dst_path = str(Path(workdir) / "dry_run.db")
    src, dst = sqlite3.connect(db), sqlite3.connect(dst_path)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    return dst_path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m arena.main", description="AI Trading League")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("name", nargs="?", help="tool name (tool command)")
    parser.add_argument("--args", help="JSON arguments (tool command)")
    parser.add_argument("--dry-run", action="store_true", help="run on a throwaway copy of the database")
    parser.add_argument("--tickers", help="comma-separated subset (update-prices)")
    parser.add_argument("--date", help="YYYY-MM-DD: decision date (kickoff/decide) or day (execute/daily/tool)")
    parser.add_argument("--no-update", action="store_true", help="skip downloading prices first")
    parser.add_argument("--start", help="simulate: first week to replay (YYYY-MM-DD)")
    parser.add_argument("--weeks", type=int, help="simulate: number of weeks")
    parser.add_argument("--yes", action="store_true", help="simulate: skip the >4 weeks confirmation")
    parser.add_argument("--sim-db", default=str(SETTINGS.db_path.with_name("simulation.db")),
                        help="simulate: database file for the simulation")
    parser.add_argument("--probe", action="store_true", help="check-agents: run a tiny test turn")
    parser.add_argument("--apply", action="store_true", help="mirror: actually place the paper orders")
    parser.add_argument("--no-browser", action="store_true", help="app: don't open the browser")
    parser.add_argument("--host", default="127.0.0.1", help="serve: address to listen on")
    parser.add_argument("--port", type=int, default=8000, help="serve: port")
    parser.add_argument("--week", type=int, help="reports: the week to (re)write")
    parser.add_argument("--force", action="store_true", help="reports: redo even if one exists")
    parser.add_argument("--reports", action="store_true", help="simulate: also run the weekly report turns")
    parser.add_argument("--source", help="refresh-universe: URL or CSV file (Symbol, Security, GICS Sector)")
    parser.add_argument("--to", help="restore-backup: where to write the restored database")
    parser.add_argument("--reason", help="check-logins: shown in the alert (e.g. 'before decide')")
    parser.add_argument("--scheduler", action="store_true", help="health: also check the scheduler heartbeat")
    parser.add_argument("--db", default=str(SETTINGS.db_path), help="database path")
    args = parser.parse_args(argv)
    import os
    os.environ.setdefault("ARENA_DB", str(Path(args.db).resolve()))   # where alerts keep their state
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    if args.command == "health":
        # Runs every 30 s from Docker: no database connection/migration, no network.
        from .health import check
        result = check(args.db, scheduler=args.scheduler)
        print(json.dumps(result))
        sys.exit(0 if result["ok"] else 1)
    if args.command == "restore-backup":
        # Must not open (and so create or migrate) the database it may replace.
        try:
            cmd_restore_backup(None, args)
        except Exception as e:  # noqa: BLE001
            sys.exit(f"Restore failed: {type(e).__name__}: {e}")
        return
    if args.command == "hash-password":
        # Never touches the database (on a fresh server it must not create an empty one).
        cmd_hash_password(None, args)
        return

    tmp = None
    if args.dry_run and args.command not in READ_ONLY:
        tmp = tempfile.mkdtemp(prefix="arena_dry_")
        if Path(args.db).exists():
            args.db = _throwaway_copy(args.db, tmp)
        else:
            args.db = str(Path(tmp) / "dry_run.db")
        args.sim_db = str(Path(tmp) / "simulation.db")
    conn = connect(args.db)
    try:
        COMMANDS[args.command](conn, args)
        if tmp:
            print("(dry run: worked on a throwaway copy; your database was not changed)")
    except MissingKeyError as e:
        sys.exit(f"Configuration error: {e}")
    except league.LoginProblem as e:
        # Exit code 75 (EX_TEMPFAIL) tells the scheduler "retry until fixed";
        # it doesn't count towards the normal 3-attempt limit.
        print(f"Can't finish {args.command}: login problem: {e}", file=sys.stderr)
        sys.exit(75)
    except LeagueError as e:
        sys.exit(f"Can't run {args.command}: {e}")
    finally:
        conn.close()
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
