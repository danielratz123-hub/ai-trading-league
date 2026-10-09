"""Weekly and daily league cycle.

    decide  (weekend)       agents research + submit -> pending rebalance orders
    execute (next trading   risk exits -> mark at open -> score last week (+bonus)
             day, after its    -> rebalance -> post_trade snapshot
             open bar exists)
    daily   (each close)    borrow fees, stops/take-profits, forced covers, snapshot

Agents only ever *propose*: decision.check validates, the ledger executes.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile

import pandas as pd
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from . import ledger, risk, scoring
from .agents import make_agent
from .agents import briefing as brief
from .agents.base import AgentAdapter, TurnOutcome, Usage, run_turn, stopped_reason, to_json
from .agents.cli import CliAgent, CliRun, mcp_launch
from .agents.decision import check, targets_from_json, targets_to_json
from .config import SETTINGS
from .db import connect, log_event, transaction
from .prices import ET, get_price, latest_price_date, store_opening_prices, update_prices
from .tools.context import ResearchContext
from .tools.registry import Budget, ToolBox
from .trading_calendar import next_trading_day, trading_days

AgentFactory = Callable[[str], "AgentAdapter | CliAgent"]


class LeagueError(RuntimeError):
    """A command can't run right now; the message says why and what to do."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Pause
# --------------------------------------------------------------------------

def is_paused(conn) -> bool:
    row = conn.execute("SELECT value FROM league_state WHERE key='paused'").fetchone()
    return bool(row and row[0] == "1")


def set_paused(conn, paused: bool) -> None:
    with transaction(conn):
        conn.execute("INSERT OR REPLACE INTO league_state(key, value) VALUES ('paused', ?)",
                     ("1" if paused else "0",))
        log_event(conn, "info", "league", "paused" if paused else "resumed")


def _require_running(conn) -> None:
    if is_paused(conn):
        raise LeagueError("The league is paused. Run `resume` to continue.")


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------

def week_end_on_or_before(conn, d: str) -> str:
    """Last trading day of the most recent *completed* trading week <= d."""
    days = trading_days(conn, "1900-01-01", d)
    if not days:
        raise LeagueError(f"No trading days on or before {d}.")
    for i in range(len(days) - 1, -1, -1):
        cur = days[i]
        nxt = next_trading_day(conn, cur)
        if date.fromisoformat(nxt).isocalendar()[:2] != date.fromisoformat(cur).isocalendar()[:2]:
            return cur
    raise LeagueError("Could not find a completed trading week.")


def default_decision_date(conn) -> str:
    last = latest_price_date(conn)
    if last is None:
        raise LeagueError("No prices yet. Run update-prices first.")
    d = week_end_on_or_before(conn, last)
    if d != last:
        raise LeagueError(f"Latest prices are from {last}, mid-week. Decisions use the last close of a "
                          f"week; run decide on the weekend (or pass --date {d}).")
    return d


# --------------------------------------------------------------------------
# kickoff / decide
# --------------------------------------------------------------------------

def ensure_agents(conn, on_date: str) -> list[str]:
    created = []
    for a in SETTINGS.agents:
        if conn.execute("SELECT 1 FROM agents WHERE agent_id=?", (a,)).fetchone() is None:
            ledger.create_agent(conn, a, SETTINGS.starting_cash, on_date, SETTINGS.models.get(a))
            created.append(a)
    return created


@dataclass
class TurnReport:
    agent_id: str
    status: str
    cost_usd: float
    tool_calls: int
    web_searches: int
    attempts: int
    error: str | None = None
    message: str = ""


def decide(conn: sqlite3.Connection, decision_date: str | None = None, simulate: bool = False,
           factory: AgentFactory = make_agent) -> list[TurnReport]:
    _require_running(conn)
    decision_date = decision_date or default_decision_date(conn)
    if get_price(conn, "SPY", decision_date, "close") is None:
        raise LeagueError(f"No prices for {decision_date}. Run update-prices first.")
    skip_stale_weeks(conn, decision_date)
    exec_date = next_trading_day(conn, decision_date)
    # Build the adapters first so a missing API key fails before anything is written.
    adapters = {a: factory(a) for a in SETTINGS.agents}

    with transaction(conn):
        ensure_agents(conn, decision_date)
        row = conn.execute("SELECT * FROM weeks WHERE decision_date=?", (decision_date,)).fetchone()
        if row is None:
            week_no = (conn.execute("SELECT COALESCE(MAX(week_no), 0) FROM weeks").fetchone()[0]) + 1
            conn.execute("INSERT INTO weeks(week_no, decision_date, exec_date, status, simulate) "
                         "VALUES (?,?,?,'deciding',?)", (week_no, decision_date, exec_date, int(simulate)))
        else:
            week_no = row["week_no"]
            if row["status"] not in ("deciding",):
                raise LeagueError(f"Week {week_no} ({decision_date}) is already {row['status']}.")

    reports = []
    for agent_id in ledger.agent_ids(conn):
        done = conn.execute("SELECT id, status FROM agent_turns WHERE week_no=? AND agent_id=?",
                            (week_no, agent_id)).fetchone()
        if done and done["status"] != "running":
            # Re-running decide only fills in agents that haven't finished yet.
            reports.append(TurnReport(agent_id, f"already {done['status']}", 0, 0, 0, 0))
            continue
        if done:
            # Left 'running' by a run that was interrupted (crash, reboot):
            # nothing was decided, so start that agent's turn over.
            with transaction(conn):
                conn.execute("DELETE FROM agent_turns WHERE id=?", (done["id"],))
        adapter = adapters.get(agent_id)
        if adapter is None:
            try:
                adapter = factory(agent_id)
            except Exception as e:  # noqa: BLE001 - one agent without an adapter mustn't stop the rest
                reports.append(TurnReport(agent_id, "failed", 0, 0, 0, 0, f"No adapter: {e}"))
                continue
        run = _run_cli_turn if isinstance(adapter, CliAgent) else _run_agent_turn
        reports.append(run(conn, adapter, week_no, decision_date, exec_date, simulate))

    with transaction(conn):
        conn.execute("UPDATE weeks SET status='decided' WHERE week_no=?", (week_no,))
    return reports


def skip_stale_weeks(conn: sqlite3.Connection, decision_date: str) -> list[int]:
    """Close weeks from before `decision_date` that never finished: decide
    was interrupted, or the orders were never executed. Their orders can't be
    filled fairly any more (the prices since are known), so the week is
    marked 'skipped' and portfolios stay as they were; the next scoring
    covers the gap. Without this, one interrupted week would block the
    league forever."""
    stale = conn.execute("SELECT * FROM weeks WHERE status IN ('deciding','decided') AND decision_date < ? "
                         "ORDER BY week_no", (decision_date,)).fetchall()
    with transaction(conn):
        for w in stale:
            conn.execute("UPDATE weeks SET status='skipped' WHERE week_no=?", (w["week_no"],))
            conn.execute("UPDATE agent_turns SET status='failed', error=COALESCE(error, ?) "
                         "WHERE week_no=? AND status='running'",
                         ("Turn was interrupted; week skipped.", w["week_no"]))
            conn.execute("UPDATE pending_orders SET status='skipped', note='week skipped: never executed' "
                         "WHERE kind='rebalance' AND status='pending' AND created_date=?", (w["decision_date"],))
            log_event(conn, "warning", "league",
                      f"week {w['week_no']} ({w['decision_date']}) was never finished; skipped")
    return [w["week_no"] for w in stale]


def _run_agent_turn(conn, adapter: AgentAdapter, week_no: int, decision_date: str,
                    exec_date: str, simulate: bool) -> TurnReport:
    agent_id = adapter.agent_id
    ctx = ResearchContext(conn, agent_id, decision_date, simulate=simulate)
    toolbox = ToolBox(ctx, Budget())
    try:
        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO agent_turns(week_no, agent_id, model, status, started_at) VALUES (?,?,?,?,?)",
                (week_no, agent_id, adapter.model, "running", _now()))
            turn_id = cur.lastrowid
            seq = [0]

            def log(role, content):
                seq[0] += 1
                conn.execute("INSERT INTO turn_messages(turn_id, seq, role, content) VALUES (?,?,?,?)",
                             (turn_id, seq[0], role, content if isinstance(content, str) else to_json(content)))

            def submit(args: dict, attempt: int):
                checked = check(ctx, args)
                conn.execute("INSERT INTO decision_attempts(turn_id, attempt, ok, decision_json, errors) "
                             "VALUES (?,?,?,?,?)",
                             (turn_id, attempt, int(checked.ok), json.dumps(args), json.dumps(checked.errors)))
                return checked

            briefing = brief.build(conn, agent_id, week_no, decision_date, exec_date, simulate,
                                   toolbox.budget.max_calls, toolbox.budget.max_spend_usd)
            out: TurnOutcome = run_turn(adapter, toolbox, brief.system_prompt(agent_id), briefing, submit, log)

            status = "accepted" if out.accepted else "failed"
            if out.accepted:
                for r in out.accepted.slot_runs:
                    conn.execute("INSERT INTO slot_runs(week_no, agent_id, slot, ok, weights_json, error) "
                                 "VALUES (?,?,?,?,?,?)",
                                 (week_no, agent_id, r["slot"], int(r["ok"]), json.dumps(r["weights"]), r["error"]))
                conn.execute(
                    "INSERT INTO pending_orders(agent_id, created_date, exec_date, kind, payload, note) "
                    "VALUES (?,?,?,'rebalance',?,?)",
                    (agent_id, decision_date, exec_date, targets_to_json(out.accepted.targets), f"week {week_no}"))
            conn.execute(
                "UPDATE agent_turns SET status=?, error=?, decision_json=?, attempts=?, tool_calls=?, "
                "web_searches=?, model_calls=?, input_tokens=?, cached_tokens=?, cache_write_tokens=?, "
                "output_tokens=?, cost_usd=?, finished_at=? WHERE id=?",
                (status, out.error, json.dumps(out.decision) if out.decision else None, out.attempts,
                 out.tool_calls, out.web_searches, out.model_calls, out.usage.input, out.usage.cached,
                 out.usage.cache_write, out.usage.output, out.cost_usd, _now(), turn_id))
            log_event(conn, "info" if out.accepted else "error", f"decide:{agent_id}",
                      f"week {week_no}: {status} (${out.cost_usd:.3f}){' - ' + out.error if out.error else ''}")
    finally:
        ctx.close_session()
    return TurnReport(agent_id, status, out.cost_usd, out.tool_calls, out.web_searches, out.attempts,
                      out.error, out.accepted.message if out.accepted else "")


CLI_NOTE = ("Your research tools and submit_decision come from the MCP server named \"arena\" (your "
            "client may show them with a prefix, e.g. mcp__arena__screen). They are the only tools "
            "you need. Your turn is over once submit_decision accepts your decision.")
CLI_NUDGE = ("You don't have an accepted decision yet. Call the arena submit_decision tool now with your "
             "final portfolio (an empty slots list keeps everything in cash).")


def _read_result(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _run_cli_turn(conn, agent: CliAgent, week_no: int, decision_date: str, exec_date: str,
                  simulate: bool) -> TurnReport:
    """One weekly turn through Claude Code / Codex. The CLI runs as its own
    process and talks to our MCP server, which validates submissions and
    writes the outcome to result.json. We hold no DB transaction meanwhile,
    so the MCP server can write its logs."""
    db_file = conn.execute("PRAGMA database_list").fetchone()["file"]
    if not db_file:
        raise LeagueError("CLI agents need a database file, not an in-memory database.")
    agent_id = agent.agent_id
    with transaction(conn):
        turn_id = conn.execute(
            "INSERT INTO agent_turns(week_no, agent_id, model, status, started_at) VALUES (?,?,?,?,?)",
            (week_no, agent_id, agent.model or "(cli default)", "running", _now())).lastrowid
        briefing = brief.build(conn, agent_id, week_no, decision_date, exec_date, simulate,
                               SETTINGS.max_tool_calls, 0.0)
    prompt = f"{brief.system_prompt(agent_id)}\n\n{CLI_NOTE}\n\n{briefing}"

    workdir = Path(tempfile.mkdtemp(prefix=f"arena_{agent_id}_"))
    result_path = workdir / "result.json"
    mcp = mcp_launch(db_file, agent_id, decision_date, week_no, turn_id, result_path, simulate)
    runs: list[CliRun] = []
    state: dict = {}
    try:
        runs.append(agent.run(prompt, workdir, mcp, web_search=not simulate))
        state = _read_result(result_path)
        for _ in range(SETTINGS.max_nudges):
            last = runs[-1]
            if (state.get("accepted") or not last.session_id or "timed out" in (last.error or "")
                    or state.get("attempts", 0) > SETTINGS.max_decision_retries):
                break
            runs.append(agent.run(CLI_NUDGE, workdir, mcp, web_search=not simulate,
                                  resume_session=last.session_id))
            state = _read_result(result_path)
    except Exception as e:  # noqa: BLE001 - record the failure; never leave the turn 'running'
        runs.append(CliRun(error=f"Turn crashed: {type(e).__name__}: {e}"))
        state = state or _read_result(result_path)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    accepted = bool(state.get("accepted"))
    if accepted:
        error = None
    elif not state:
        error = runs[-1].error or "The agent's tool server never started."
    elif state.get("attempts", 0) > SETTINGS.max_decision_retries:
        error = ("Decision failed validation after all retries: "
                 + "; ".join(e.splitlines()[0] for e in state.get("last_errors", []))[:400])
    else:
        error = runs[-1].error or stopped_reason(state.get("attempts", 0), state.get("last_errors", []))
    shell = sum(r.shell_commands for r in runs)
    searches = sum(r.web_searches for r in runs)
    usage = Usage()
    for r in runs:
        usage.add(r.usage)
    with transaction(conn):
        conn.execute("INSERT INTO turn_messages(turn_id, seq, role, content) VALUES (?,?,?,?)",
                     (turn_id, 0, "user", prompt))
        seq = 0
        for i, r in enumerate(runs):
            for ev in r.events:
                seq += 1
                conn.execute("INSERT INTO turn_messages(turn_id, seq, role, content) VALUES (?,?,?,?)",
                             (turn_id, seq, f"cli_run{i}", json.dumps(ev)[:50_000]))
        if accepted:
            for sr in state.get("slot_runs", []):
                conn.execute("INSERT INTO slot_runs(week_no, agent_id, slot, ok, weights_json, error) "
                             "VALUES (?,?,?,?,?,?)",
                             (week_no, agent_id, sr["slot"], int(sr["ok"]), json.dumps(sr["weights"]), sr["error"]))
            conn.execute("INSERT INTO pending_orders(agent_id, created_date, exec_date, kind, payload, note) "
                         "VALUES (?,?,?,'rebalance',?,?)",
                         (agent_id, decision_date, exec_date, state["targets"], f"week {week_no}"))
        conn.execute(
            "UPDATE agent_turns SET status=?, error=?, decision_json=?, attempts=?, tool_calls=?, "
            "web_searches=?, model_calls=?, input_tokens=?, cached_tokens=?, cache_write_tokens=?, "
            "output_tokens=?, cost_usd=?, finished_at=? WHERE id=?",
            ("accepted" if accepted else "failed", error,
             json.dumps(state.get("decision")) if accepted else None, state.get("attempts", 0),
             state.get("tool_calls", 0), searches, len(runs), usage.input, usage.cached,
             usage.cache_write, usage.output, 0.0, _now(), turn_id))
        if shell:
            log_event(conn, "error", f"decide:{agent_id}",
                      f"SECURITY: the CLI ran {shell} shell command(s) despite being locked down.")
        log_event(conn, "info" if accepted else "error", f"decide:{agent_id}",
                  f"week {week_no}: {'accepted' if accepted else 'failed'} via CLI"
                  f"{' - ' + error if error else ''}")
    return TurnReport(agent_id, "accepted" if accepted else "failed", 0.0, state.get("tool_calls", 0),
                      searches, state.get("attempts", 0), error)


# --------------------------------------------------------------------------
# execute
# --------------------------------------------------------------------------

@dataclass
class ExecReport:
    week_no: int
    exec_date: str
    scored_week: int | None
    weekly_returns: dict[str, float]
    bonus_winner: str | None
    trades: dict[str, int]


def _mark_and_score(conn, exec_date: str) -> tuple[int | None, dict[str, float], str | None]:
    """Mark every agent at exec_date's open and score the last executed week."""
    for a in ledger.agent_ids(conn):
        risk.execute_pending(conn, a, exec_date)   # stops etc. due at this open go first
    marks = {a: ledger.snapshot(conn, a, exec_date, "mark", "open") for a in ledger.agent_ids(conn)}
    mark_ids = {a: ledger.latest_snapshot(conn, a, "mark")["id"] for a in marks}
    prev = conn.execute("SELECT * FROM weeks WHERE status='executed' ORDER BY week_no DESC LIMIT 1").fetchone()
    if prev is None:
        return None, {}, None
    rets = scoring.score_week(conn, prev["week_no"], prev["exec_date"], mark_ids)
    winner = scoring.maybe_award_bonus(conn, prev["week_no"], exec_date)
    return prev["week_no"], rets, winner


def execute(conn: sqlite3.Connection) -> ExecReport:
    _require_running(conn)
    week = conn.execute("SELECT * FROM weeks WHERE status='decided' ORDER BY week_no LIMIT 1").fetchone()
    if week is None:
        raise LeagueError("Nothing to execute: no decided week is waiting. Run decide first.")
    d = week["exec_date"]
    if get_price(conn, "SPY", d, "open") is None:
        raise LeagueError(f"No opening prices for {d} yet. Run execute after {d}'s open "
                          f"(9:50 ET or later) so the opening prices can be fetched.")
    with transaction(conn):
        scored, rets, winner = _mark_and_score(conn, d)
        trades = {}
        for a in ledger.agent_ids(conn):
            orders = conn.execute("SELECT * FROM pending_orders WHERE agent_id=? AND kind='rebalance' "
                                  "AND status='pending' AND exec_date=?", (a, d)).fetchall()
            n = 0
            for o in orders:
                res = ledger.rebalance(conn, a, d, targets_from_json(o["payload"]))
                n += len(res.trades)
                note = "; ".join(res.skipped) or None
                conn.execute("UPDATE pending_orders SET status='done', note=COALESCE(?, note) WHERE id=?",
                             (note, o["id"]))
            trades[a] = n
            ledger.snapshot(conn, a, d, "post_trade", "open")
        conn.execute("UPDATE weeks SET status='executed' WHERE week_no=?", (week["week_no"],))
    return ExecReport(week["week_no"], d, scored, rets, winner, trades)


def tickers_needed_at_open(conn: sqlite3.Connection, d: str) -> list[str]:
    """Everything that may trade or be valued at d's open."""
    tickers = {"SPY"}
    tickers |= {r[0] for r in conn.execute("SELECT DISTINCT ticker FROM positions")}
    tickers |= {r[0] for r in conn.execute(
        "SELECT DISTINCT ticker FROM pending_orders WHERE status='pending' AND ticker IS NOT NULL")}
    for (payload,) in conn.execute("SELECT payload FROM pending_orders WHERE status='pending' "
                                   "AND kind='rebalance' AND exec_date<=?", (d,)):
        for t in targets_from_json(payload):
            tickers |= set(t.weights)
    return sorted(tickers)


@dataclass
class OpenReport:
    weekly: ExecReport | None                 # set on execution days
    risk_fills: dict[str, int]                 # stop/take-profit/forced-cover fills per agent


def _move_execution(conn, week, new_date: str, why: str) -> None:
    with transaction(conn):
        conn.execute("UPDATE weeks SET exec_date=? WHERE week_no=?", (new_date, week["week_no"]))
        conn.execute("UPDATE pending_orders SET exec_date=? WHERE kind='rebalance' AND status='pending' "
                     "AND created_date=?", (new_date, week["decision_date"]))
        log_event(conn, "warning", "league",
                  f"week {week['week_no']}: execution moved {week['exec_date']} -> {new_date} ({why})")


def run_open(conn: sqlite3.Connection, d: str) -> OpenReport:
    """The morning job, run right after the open on every trading day:
    pending risk orders fill at today's open, and if a decided week is due,
    it's scored and rebalanced at today's open too.

    If the planned execution day was missed (machine off, data outage), the
    week executes at the first open we actually reach, never at the old
    day's price: by now that day's outcome is known, so filling there would
    be look-ahead."""
    week = conn.execute("SELECT * FROM weeks WHERE status='decided' AND exec_date<=? ORDER BY week_no LIMIT 1",
                        (d,)).fetchone()
    if week and _day_closed(conn, d):
        # Today's close was already processed, so "today's open" is history too.
        _move_execution(conn, week, next_trading_day(conn, d), "day already closed")
        week = None
    elif week and _decided_after_open(conn, week["week_no"], d):
        # The decisions were made after this morning's open (e.g. a mid-week
        # kickoff): filling at that open would use a price from before the
        # decision. The next open is the first fair one.
        _move_execution(conn, week, next_trading_day(conn, d), "decided after this open")
        week = None
    elif week and week["exec_date"] < d:
        _move_execution(conn, week, d, "planned execution day was missed")
    if week:
        before = {a: _count_risk_fills(conn, a) for a in ledger.agent_ids(conn)}
        rep = execute(conn)
        return OpenReport(rep, {a: _count_risk_fills(conn, a) - n for a, n in before.items()})
    fills = {}
    with transaction(conn):
        for a in ledger.agent_ids(conn):
            fills[a] = len(risk.execute_pending(conn, a, d))
    return OpenReport(None, fills)


def _decided_after_open(conn, week_no: int, d: str) -> bool:
    """True if any agent's turn for the week finished after d's market open."""
    last = conn.execute("SELECT MAX(finished_at) FROM agent_turns WHERE week_no=?", (week_no,)).fetchone()[0]
    row = conn.execute("SELECT open_time FROM calendar WHERE date=?", (d,)).fetchone()
    if not last or not row:
        return False
    h, m = map(int, row[0].split(":"))
    opened = datetime.fromisoformat(d).replace(hour=h, minute=m, tzinfo=ET)
    return datetime.fromisoformat(last) > opened


def _day_closed(conn, d: str) -> bool:
    return conn.execute("SELECT 1 FROM snapshots WHERE kind='daily' AND date=? LIMIT 1", (d,)).fetchone() is not None


def prepare_open(conn: sqlite3.Connection, d: str, source) -> list[str]:
    """Get everything ready to trade at d's open; returns tickers without an
    opening price.

    Daily history is brought up to date FIRST: Alpaca adjusts history as of
    the moment you ask, so if a split or dividend takes effect today, this
    is where it's detected and held shares are rescaled, before we use
    today's opening prices (which are already on the new scale)."""
    tickers = tickers_needed_at_open(conn, d)
    update_prices(conn, source, tickers, end=d, on_adjust=ledger.apply_price_adjustment)
    opens = source.fetch_opens(tickers, d)
    store_opening_prices(conn, opens, d, f"alpaca-{getattr(source, 'feed', 'unknown')}-1min")
    return sorted(set(tickers) - set(opens["ticker"]))


def reset_unexecuted_start(conn: sqlite3.Connection) -> int:
    """Throw away the league's first week if it was decided but never traded,
    so the agents can decide it again (e.g. with fresher data). Refuses once
    any money has moved. Returns the number of weeks removed."""
    if conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]:
        raise LeagueError("Trades have already happened; the start can't be redone.")
    weeks = conn.execute("SELECT * FROM weeks").fetchall()
    if any(w["status"] not in ("deciding", "decided", "skipped") for w in weeks):
        raise LeagueError("A week was already executed; the start can't be redone.")
    with transaction(conn):
        for w in weeks:
            turns = [r[0] for r in conn.execute("SELECT id FROM agent_turns WHERE week_no=?", (w["week_no"],))]
            for t in turns:
                conn.execute("DELETE FROM turn_messages WHERE turn_id=?", (t,))
                conn.execute("DELETE FROM decision_attempts WHERE turn_id=?", (t,))
            conn.execute("DELETE FROM agent_turns WHERE week_no=?", (w["week_no"],))
            conn.execute("DELETE FROM slot_runs WHERE week_no=?", (w["week_no"],))
            conn.execute("DELETE FROM tool_calls WHERE decision_date=?", (w["decision_date"],))
        conn.execute("DELETE FROM pending_orders WHERE kind='rebalance'")
        conn.execute("DELETE FROM weeks")
        log_event(conn, "info", "league", f"start redone: {len(weeks)} unexecuted week(s) removed")
    return len(weeks)


def execute_now(conn: sqlite3.Connection, d: str, source) -> ExecReport:
    """Trade the waiting week right now, at the latest market price, instead
    of waiting for the next open (used to start the league mid-day).

    Fair only if every price is newer than every decision: that's checked,
    so no fill can use a price the agents could have seen before deciding."""
    _require_running(conn)
    week = conn.execute("SELECT * FROM weeks WHERE status='decided' ORDER BY week_no LIMIT 1").fetchone()
    if week is None:
        raise LeagueError("Nothing to trade: no decided week is waiting.")
    decided = conn.execute("SELECT MAX(finished_at) FROM agent_turns WHERE week_no=?", (week["week_no"],)).fetchone()[0]
    if not decided:
        raise LeagueError("The week has no finished agent turns.")
    decided_at = datetime.fromisoformat(decided)
    tickers = tickers_needed_at_open(conn, week["exec_date"])
    update_prices(conn, source, tickers, end=d, on_adjust=ledger.apply_price_adjustment)
    latest = source.fetch_latest(tickers, decided_at)
    if latest.empty:
        raise LeagueError("No market prices since the decisions were made. Is the market open?")
    stale = latest[latest["time"] <= pd.Timestamp(decided_at)]
    if not stale.empty:
        raise LeagueError(f"Prices for {sorted(stale['ticker'])} are older than the decisions; try again in a minute.")
    stamp = latest["time"].max().tz_convert(ET).strftime("%H:%M")
    store_opening_prices(conn, latest.rename(columns={"price": "open"})[["ticker", "open"]], d,
                         f"alpaca-{getattr(source, 'feed', 'unknown')}-market-{stamp}ET")
    if week["exec_date"] != d:
        _move_execution(conn, week, d, "traded immediately at the market price")
    return execute(conn)


def complete_week_now(conn: sqlite3.Connection, d: str, source) -> list[str]:
    """After an execution, at the latest market price:
      1. buy what the plan wanted but couldn't be filled (no price at the
         time), at the weights the agent chose;
      2. bring any slot whose cash went below zero back to zero, by trimming
         its largest long.
    Prices must be newer than the decisions. Returns what was done."""
    _require_running(conn)
    week = conn.execute("SELECT * FROM weeks WHERE status IN ('executed','scored') "
                        "ORDER BY week_no DESC LIMIT 1").fetchone()
    if week is None:
        raise LeagueError("No executed week to complete.")
    decided = conn.execute("SELECT MAX(finished_at) FROM agent_turns WHERE week_no=?", (week["week_no"],)).fetchone()[0]
    after = max(datetime.fromisoformat(decided), datetime.now(ET) - pd.Timedelta(minutes=30))
    plans = {r["agent_id"]: targets_from_json(r["payload"]) for r in conn.execute(
        "SELECT agent_id, payload FROM pending_orders WHERE kind='rebalance' AND status='done' AND created_date=?",
        (week["decision_date"],))}
    held = {(p["agent_id"], p["slot"], p["ticker"]) for a in ledger.agent_ids(conn) for p in ledger.positions(conn, a)}
    missing = [(a, t.slot, ticker, w, t.allocation) for a, targets in plans.items() for t in targets
               for ticker, w in t.weights.items() if w and (a, t.slot, ticker) not in held]
    negative = conn.execute("SELECT agent_id, slot, cash FROM slots WHERE cash < -0.005").fetchall()
    if not missing and not negative:
        return []
    tickers = sorted({m[2] for m in missing} | {p[2] for p in held})
    quotes = source.fetch_latest(tickers, after)
    fresh = {r.ticker: float(r.price) for r in quotes.itertuples() if r.time > pd.Timestamp(datetime.fromisoformat(decided))}
    done = []
    with transaction(conn):
        # Today's "open" prices are what the league traded at; add the fresh
        # quotes for tickers that had none, so everything is valued on the
        # same footing (not at yesterday's close).
        conn.executemany("INSERT OR IGNORE INTO opening_prices(ticker, date, open, source) VALUES (?,?,?,?)",
                         [(t, d, px, "complete-now") for t, px in fresh.items()])
        for a, slot, ticker, w, alloc in missing:
            px = fresh.get(ticker)
            if px is None:
                done.append(f"{a}: {ticker} still has no fresh price; left in cash")
                continue
            equity = ledger.value_agent(conn, a, d, "open").equity
            shares = ledger.affordable(conn, a, slot, w * alloc * equity / px, px)
            plan = next((t for t in plans.get(a, []) if t.slot == slot), None)
            tr = ledger.execute_trade(conn, a, slot, ticker, shares, d, px, "rebalance",
                                      ledger.trade_note(plan, ticker) + " (bought later: no price at first)")
            if tr:
                done.append(f"{a}: bought {tr.shares:.4f} {ticker} @ {tr.fill_price:.2f} (planned {w:.0%} of slot {slot})")
        for r in conn.execute("SELECT agent_id, slot, cash FROM slots WHERE cash < -0.005").fetchall():
            longs = [(p["shares"] * fresh.get(p["ticker"], 0), p) for p in ledger.positions(conn, r["agent_id"])
                     if p["slot"] == r["slot"] and p["shares"] > 0 and p["ticker"] in fresh]
            if not longs:
                continue
            _, p = max(longs, key=lambda x: x[0])
            px = fresh[p["ticker"]]
            # Sell a little more than the shortfall so its own costs are covered too, at least $1.
            shares = max(-r["cash"] * 1.02 + 0.05, costs_min_trade()) / px
            tr = ledger.execute_trade(conn, r["agent_id"], r["slot"], p["ticker"], -shares, d, px, "rebalance",
                                      "Engine: small trim so trading costs don't leave the cash below $0.")
            if tr:
                done.append(f"{r['agent_id']}: sold {-tr.shares:.4f} {p['ticker']} to bring slot {r['slot']} cash back above $0")
        for a in ledger.agent_ids(conn):
            ledger.snapshot(conn, a, d, "post_trade", "open")
        log_event(conn, "info", "league", "completed week now: " + "; ".join(done))
    return done


def costs_min_trade() -> float:
    from .config import SETTINGS as S
    return S.costs.min_trade_value + 0.01


def _count_risk_fills(conn, agent_id: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM trades WHERE agent_id=? AND reason != 'rebalance'",
                        (agent_id,)).fetchone()[0]


def close_out(conn: sqlite3.Connection, on_date: str) -> ExecReport:
    """Score the last executed week without starting a new one (end of a
    simulation): mark at on_date's open, score, no trades."""
    with transaction(conn):
        scored, rets, winner = _mark_and_score(conn, on_date)
    return ExecReport(0, on_date, scored, rets, winner, {})


# --------------------------------------------------------------------------
# daily
# --------------------------------------------------------------------------

def daily(conn: sqlite3.Connection, d: str) -> dict[str, risk.DailyReport]:
    """End-of-day processing. If a week was due to execute today but
    execute didn't run, the week simply moves to the next open (run_open)."""
    return {a: risk.process_daily(conn, a, d) for a in ledger.agent_ids(conn)}


# --------------------------------------------------------------------------
# simulate
# --------------------------------------------------------------------------

def simulation_dates(conn, start: str, weeks: int) -> list[str]:
    """Decision dates (week ends) for `weeks` weeks starting at `start`."""
    days = trading_days(conn, start, "2999-12-31")
    out = []
    for d in days:
        if len(out) == weeks:
            break
        nxt = next_trading_day(conn, d)
        if date.fromisoformat(nxt).isocalendar()[:2] != date.fromisoformat(d).isocalendar()[:2]:
            out.append(d)
    return out


def new_simulation_db(source: sqlite3.Connection, path: Path) -> sqlite3.Connection:
    """A separate DB with a copy of prices + calendar, so a simulation never
    touches the live league."""
    if path.exists():
        path.unlink()
    sim = connect(path)
    src_file = source.execute("PRAGMA database_list").fetchone()["file"]
    sim.execute("ATTACH DATABASE ? AS src", (src_file,))
    with transaction(sim):
        sim.execute("INSERT INTO prices SELECT * FROM src.prices")
        sim.execute("INSERT INTO calendar SELECT * FROM src.calendar")
    sim.execute("DETACH DATABASE src")
    return sim


def simulate(conn: sqlite3.Connection, start: str, weeks: int, factory: AgentFactory = make_agent,
             progress: Callable[[str], None] = print) -> list[dict]:
    """Replay `weeks` past weeks with real agent turns on `conn` (use a
    simulation DB!). Web search is off and every tool is cut at the
    simulated decision date."""
    dates = simulation_dates(conn, start, weeks + 1)
    if len(dates) < weeks + 1:
        raise LeagueError(f"Not enough price history after {start} for {weeks} weeks plus the week "
                          f"needed to score the last one.")
    # The last decision is held for one more week, then scored at the open
    # after that week ends.
    final_mark = next_trading_day(conn, dates[weeks])
    if get_price(conn, "SPY", final_mark, "open") is None:
        raise LeagueError(f"Need prices through {final_mark} to score the final week.")

    summary = []
    for i in range(weeks):
        dd = dates[i]
        progress(f"Week {i + 1}/{weeks}: deciding with data up to {dd} ...")
        for r in decide(conn, dd, simulate=True, factory=factory):
            progress(f"  {r.agent_id}: {r.status} (${r.cost_usd:.3f}, {r.tool_calls} tools, "
                     f"{r.attempts} submits){' - ' + r.error if r.error else ''}")
        rep = execute(conn)
        if rep.scored_week:
            progress(f"  scored week {rep.scored_week}: " +
                     ", ".join(f"{a} {r:+.2%}" for a, r in rep.weekly_returns.items()) +
                     (f"; bonus to {rep.bonus_winner}" if rep.bonus_winner else ""))
        # Hold through the next week end (daily stops, fees, snapshots).
        for d in trading_days(conn, rep.exec_date, dates[i + 1]):
            if d != rep.exec_date:
                run_open(conn, d)        # stops / forced covers fill at this open
            daily(conn, d)
        summary.append({"week": rep.week_no, "decision_date": dd, "exec_date": rep.exec_date})
    final = close_out(conn, final_mark)
    progress(f"Scored final week {final.scored_week}: " +
             ", ".join(f"{a} {r:+.2%}" for a, r in final.weekly_returns.items()) +
             (f"; bonus to {final.bonus_winner}" if final.bonus_winner else ""))
    return summary


def estimated_cost(weeks: int) -> float:
    """Upper bound: every agent spends its whole budget plus the hard overrun."""
    return weeks * len(SETTINGS.agents) * SETTINGS.max_api_spend_usd * SETTINGS.hard_spend_multiplier


def backfill_trade_notes(conn: sqlite3.Connection) -> int:
    """Give trades made before trade notes existed a 'why', from the decision
    each one came from. Returns how many were filled in."""
    filled = 0
    with transaction(conn):
        for t in conn.execute("SELECT * FROM trades WHERE note IS NULL").fetchall():
            if t["reason"] != "rebalance":
                note = {"stop_loss": "Stop-loss", "take_profit": "Take-profit",
                        "forced_cover": "Forced cover (engine)"}.get(t["reason"], t["reason"])
            else:
                turn = conn.execute(
                    "SELECT a.decision_json FROM agent_turns a JOIN weeks w ON w.week_no = a.week_no "
                    "WHERE a.agent_id=? AND a.status='accepted' AND w.exec_date<=? ORDER BY a.week_no DESC LIMIT 1",
                    (t["agent_id"], t["date"])).fetchone()
                slot = None
                if turn and turn["decision_json"]:
                    slot = next((s for s in json.loads(turn["decision_json"]).get("slots", [])
                                 if s.get("slot") == t["slot"]), None)
                if slot is None:
                    note = "Slot closed: no longer part of the plan."
                else:
                    weight = (slot.get("positions") or {}).get(t["ticker"])
                    if weight and (weight > 0) != (t["shares"] > 0) and abs(t["value"]) < 5:
                        note = "Engine: small trim so trading costs don't leave the cash below $0."
                    else:
                        own = (slot.get("position_reasons") or {}).get(t["ticker"])
                        note = own or f"{slot.get('name', '')}: {slot.get('reason', '')}".strip(": ")
            conn.execute("UPDATE trades SET note=? WHERE id=?", (note[:500], t["id"]))
            filled += 1
    return filled
