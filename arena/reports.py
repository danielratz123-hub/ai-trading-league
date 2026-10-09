"""Weekly strategy reports.

After a week is scored, each agent writes a short report on its own
strategies: per slot what worked, what didn't, what to improve and what to
preserve. The NUMBERS in a report (slot return, contribution, biggest winner
and loser, costs, hit rate) are computed here from the database and merged
in by the engine; the agent only supplies the words, so it can't misreport
its own results.

The report turn is small: no web search, read-only tools on the agent's own
data (mcp_server.py --report), a tool budget and a timeout. Its token usage
is stored with the report and counted in the usage costs.

The last few reports go into the agent's briefing for its next decision,
and "what to preserve" items carry a short key so we can count how often the
same lesson comes back week after week.

The week window: week N's return runs from the post_trade snapshot on its
execution day (S0) to the mark snapshot at the next execution open (S1),
exactly like scoring. Costs also include week N's own opening rebalance
(the trades just before S0), because those trades set up the week.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .config import SETTINGS
from .db import log_event, transaction

KEY = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
TEXT_FIELDS = ("what_worked", "what_didnt", "what_to_improve")
MAX_TEXT = 1500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# The engine's numbers
# --------------------------------------------------------------------------

def _shares_at(conn, agent_id: str, slot: int, ticker: str, seq: int) -> float:
    """Shares held right after event `seq`, rebuilt from the trades, in the
    share basis of that moment (splits/adjustments up to `seq` applied)."""
    actions = conn.execute("SELECT seq, factor FROM corporate_actions WHERE ticker=? AND seq<=?",
                           (ticker, seq)).fetchall()
    total = 0.0
    for t in conn.execute("SELECT seq, shares FROM trades WHERE agent_id=? AND slot=? AND ticker=? AND seq<=?",
                          (agent_id, slot, ticker, seq)):
        f = 1.0
        for a in actions:
            if a["seq"] > t["seq"]:
                f *= a["factor"]      # apply_price_adjustment divides held shares by the factor
        total += t["shares"] / f
    return 0.0 if abs(total) < 1e-9 else total


def _open_price(conn, ticker: str, d: str) -> float | None:
    """The open as the engine used it that day (captured opening price first)."""
    from .prices import get_price
    return get_price(conn, ticker, d, "open")


def week_window(conn, agent_id: str, week_no: int) -> dict | None:
    w = conn.execute("SELECT * FROM weeks WHERE week_no=?", (week_no,)).fetchone()
    sc = conn.execute("SELECT * FROM scores WHERE week_no=? AND agent_id=?", (week_no, agent_id)).fetchone()
    if w is None or sc is None:
        return None
    s0 = conn.execute("SELECT * FROM snapshots WHERE agent_id=? AND kind='post_trade' AND date=? "
                      "ORDER BY seq DESC LIMIT 1", (agent_id, w["exec_date"])).fetchone()
    s1 = conn.execute("SELECT * FROM snapshots WHERE agent_id=? AND kind='mark' AND seq>? "
                      "ORDER BY seq LIMIT 1", (agent_id, s0["seq"] if s0 else 0)).fetchone()
    if s0 is None or s1 is None:
        return None
    pre = conn.execute("SELECT seq FROM snapshots WHERE agent_id=? AND kind='mark' AND seq<? "
                       "ORDER BY seq DESC LIMIT 1", (agent_id, s0["seq"])).fetchone()
    return {"week": w, "score": sc, "s0": s0, "s1": s1, "cost_from_seq": pre["seq"] if pre else 0}


def week_numbers(conn, agent_id: str, week_no: int) -> dict | None:
    """Everything a report states as a number, from the database."""
    win = week_window(conn, agent_id, week_no)
    if win is None:
        return None
    w, sc, s0, s1 = win["week"], win["score"], win["s0"], win["s1"]
    start_eq = sc["start_equity"]
    end_date = s1["date"]
    slots0, slots1 = json.loads(s0["slots_json"]), json.loads(s1["slots_json"])
    meta = {}
    turn = conn.execute("SELECT decision_json FROM agent_turns WHERE week_no=? AND agent_id=? AND status='accepted'",
                        (week_no, agent_id)).fetchone()
    if turn and turn["decision_json"]:
        for s in json.loads(turn["decision_json"]).get("slots", []):
            meta[int(s.get("slot", 0))] = s
    out_slots = []
    for slot_s, eq0 in sorted(slots0.items(), key=lambda kv: int(kv[0])):
        slot = int(slot_s)
        if slot == 0 or eq0 <= 0:
            continue
        tickers = {r[0] for r in conn.execute(
            "SELECT DISTINCT ticker FROM trades WHERE agent_id=? AND slot=? AND seq<=?", (agent_id, slot, s1["seq"]))}
        per = []
        for t in sorted(tickers):
            sh0 = _shares_at(conn, agent_id, slot, t, s0["seq"])
            sh1 = _shares_at(conn, agent_id, slot, t, s1["seq"])
            flows = conn.execute("SELECT COALESCE(SUM(shares*fill_price + commission), 0), COUNT(*) FROM trades "
                                 "WHERE agent_id=? AND slot=? AND ticker=? AND seq>? AND seq<=?",
                                 (agent_id, slot, t, s0["seq"], s1["seq"])).fetchone()
            if sh0 == 0 and sh1 == 0 and flows[1] == 0:
                continue                      # not held during this week
            borrow = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM borrow_fees WHERE agent_id=? AND slot=? "
                                  "AND ticker=? AND seq>? AND seq<=?",
                                  (agent_id, slot, t, s0["seq"], s1["seq"])).fetchone()[0]
            p0, p1 = _open_price(conn, t, w["exec_date"]), _open_price(conn, t, end_date)
            v0 = sh0 * (p0 or 0.0)
            v1 = sh1 * (p1 or 0.0)
            pnl = v1 - v0 - flows[0] - borrow
            per.append({"ticker": t, "pnl": round(pnl, 2), "side": "short" if (sh0 or sh1) < 0 else "long",
                        "start_value": round(v0, 2), "end_value": round(v1, 2)})
        costs = conn.execute(
            "SELECT COALESCE(SUM(commission), 0), COALESCE(SUM(slippage_cost), 0), COUNT(*) FROM trades "
            "WHERE agent_id=? AND slot=? AND seq>? AND seq<=?",
            (agent_id, slot, win["cost_from_seq"], s1["seq"])).fetchone()
        borrow_all = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM borrow_fees WHERE agent_id=? AND slot=? "
                                  "AND seq>? AND seq<=?", (agent_id, slot, s0["seq"], s1["seq"])).fetchone()[0]
        ss = conn.execute("SELECT name, weekly_return FROM slot_scores WHERE week_no=? AND agent_id=? AND slot=?",
                          (week_no, agent_id, slot)).fetchone()
        slot_pnl = slots1.get(slot_s, 0.0) - eq0
        ranked = sorted(per, key=lambda p: p["pnl"])
        m = meta.get(slot, {})
        out_slots.append({
            "slot": slot,
            "name": (ss["name"] if ss else None) or m.get("name", ""),
            "type": m.get("type", ""),
            "allocation": round(eq0 / start_eq, 4) if start_eq else None,
            "slot_return": round(ss["weekly_return"], 6) if ss else round(slot_pnl / eq0, 6),
            "pnl_usd": round(slot_pnl, 2),
            "contribution": round(slot_pnl / start_eq, 6) if start_eq else None,
            "biggest_winner": ranked[-1] if ranked and ranked[-1]["pnl"] > 0 else None,
            "biggest_loser": ranked[0] if ranked and ranked[0]["pnl"] < 0 else None,
            "hit_rate": round(sum(p["pnl"] > 0 for p in per) / len(per), 4) if per else None,
            "positions": len(per),
            "trades": costs[2],
            "costs": {"commission": round(costs[0], 2), "slippage": round(costs[1], 2),
                      "borrow": round(borrow_all, 2), "total": round(costs[0] + costs[1] + borrow_all, 2)},
            "tickers": sorted(per, key=lambda p: -abs(p["pnl"])),
        })
    return {"week": week_no, "agent": agent_id, "exec_date": w["exec_date"], "end_date": end_date,
            "weekly_return": round(sc["weekly_return"], 6), "point": sc["point"],
            "start_equity": round(start_eq, 2), "end_equity": round(sc["end_equity"], 2),
            "slots": out_slots}


def numbers_text(n: dict) -> str:
    """The numbers as the agent sees them in its report turn."""
    lines = [f"Week {n['week']} ({n['exec_date']} open -> {n['end_date']} open): weekly return "
             f"{n['weekly_return']:+.2%}, point: {'yes' if n['point'] else 'no'}, equity "
             f"${n['start_equity']:,.2f} -> ${n['end_equity']:,.2f}."]
    if not n["slots"]:
        lines.append("You had no invested slots this week (all cash).")
    for s in n["slots"]:
        bw, bl = s["biggest_winner"], s["biggest_loser"]
        lines.append(
            f"- Slot {s['slot']} \"{s['name']}\" [{s['type'] or '?'}], {s['allocation']:.0%} of equity: "
            f"return {s['slot_return']:+.2%}, P&L ${s['pnl_usd']:+,.2f}, contribution {s['contribution']:+.2%}; "
            f"best {bw['ticker'] + ' $' + format(bw['pnl'], '+,.2f') if bw else '-'}, "
            f"worst {bl['ticker'] + ' $' + format(bl['pnl'], '+,.2f') if bl else '-'}; "
            f"hit rate {'-' if s['hit_rate'] is None else format(s['hit_rate'], '.0%')} of {s['positions']} "
            f"position(s); costs ${s['costs']['total']:.2f} (commission {s['costs']['commission']:.2f}, "
            f"slippage {s['costs']['slippage']:.2f}, borrow {s['costs']['borrow']:.2f})")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# The agent's submission
# --------------------------------------------------------------------------

SUBMIT_REPORT_SPEC = {
    "name": "submit_report",
    "description": (
        "Submit your weekly strategy report. One entry per slot listed in get_week_numbers. "
        "Write in plain words; the engine adds all numbers itself. For what_to_preserve give 0-5 "
        "items, each with a short stable key (lowercase-with-dashes) - reuse the SAME key when the "
        "same lesson comes back in later weeks, so repeats can be counted."),
    "parameters": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "2-4 sentences on the week overall."},
            "slots": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "slot": {"type": "integer"},
                    "what_worked": {"type": "string"},
                    "what_didnt": {"type": "string"},
                    "what_to_improve": {"type": "string"},
                    "what_to_preserve": {"type": "array", "items": {
                        "type": "object",
                        "properties": {"key": {"type": "string"}, "text": {"type": "string"}},
                        "required": ["key", "text"]}},
                },
                "required": ["slot", "what_worked", "what_didnt", "what_to_improve", "what_to_preserve"]}},
        },
        "required": ["summary", "slots"],
    },
}


def check_report(report: dict, numbers: dict) -> list[str]:
    errors = []
    if not isinstance(report, dict):
        return ["The report must be a JSON object."]
    if not isinstance(report.get("summary"), str) or not report["summary"].strip():
        errors.append("summary is missing.")
    want = {s["slot"] for s in numbers["slots"]}
    got = []
    for s in report.get("slots") or []:
        if not isinstance(s, dict) or not isinstance(s.get("slot"), int):
            errors.append("Each slots entry needs an integer slot.")
            continue
        got.append(s["slot"])
        for f in TEXT_FIELDS:
            v = s.get(f)
            if not isinstance(v, str) or not v.strip():
                errors.append(f"Slot {s['slot']}: {f} is missing.")
            elif len(v) > MAX_TEXT:
                errors.append(f"Slot {s['slot']}: {f} is too long (max {MAX_TEXT} characters).")
        items = s.get("what_to_preserve")
        if not isinstance(items, list) or len(items) > 5:
            errors.append(f"Slot {s['slot']}: what_to_preserve must be a list of at most 5 items.")
            continue
        for it in items:
            if not isinstance(it, dict) or not KEY.match(str(it.get("key", ""))) or not str(it.get("text", "")).strip():
                errors.append(f"Slot {s['slot']}: each what_to_preserve item needs a key like "
                              f"'momentum-tilt' (lowercase, dashes, 2-40 characters) and a text.")
                break
    if sorted(got) != sorted(want):
        errors.append(f"Write exactly one entry per slot: expected slots {sorted(want)}, got {sorted(got)}.")
    return errors


def merge(report: dict, numbers: dict, repeats: dict[str, int]) -> dict:
    """The stored JSON: the engine's numbers + the agent's words."""
    words = {s["slot"]: s for s in report["slots"]}
    slots = []
    for s in numbers["slots"]:
        w = words.get(s["slot"], {})
        slots.append({**{k: v for k, v in s.items() if k != "tickers"},
                      **{f: w.get(f, "") for f in TEXT_FIELDS},
                      "what_to_preserve": [{"key": i["key"], "text": i["text"], "times": repeats.get(i["key"], 1)}
                                           for i in w.get("what_to_preserve", [])]})
    return {**{k: v for k, v in numbers.items() if k != "slots"}, "summary": report["summary"], "slots": slots}


def render_text(r: dict) -> str:
    """The readable version (stored, shown on the dashboard and in briefings)."""
    lines = [f"Week {r['week']} report ({r['agent']}): return {r['weekly_return']:+.2%}, "
             f"point {'yes' if r['point'] else 'no'}.", r["summary"].strip()]
    for s in r["slots"]:
        bw, bl = s.get("biggest_winner"), s.get("biggest_loser")
        lines += ["", f"Slot {s['slot']} \"{s['name']}\" [{s['type'] or '?'}]: return {s['slot_return']:+.2%}, "
                      f"contribution {s['contribution']:+.2%}, P&L ${s['pnl_usd']:+,.2f}, costs ${s['costs']['total']:.2f}, "
                      f"hit rate {'-' if s['hit_rate'] is None else format(s['hit_rate'], '.0%')}"
                      + (f", best {bw['ticker']} ${bw['pnl']:+,.2f}" if bw else "")
                      + (f", worst {bl['ticker']} ${bl['pnl']:+,.2f}" if bl else "") + ".",
                  f"  Worked: {s['what_worked']}",
                  f"  Didn't: {s['what_didnt']}",
                  f"  Improve: {s['what_to_improve']}"]
        for i in s["what_to_preserve"]:
            lines.append(f"  Preserve [{i['key']}] (x{i['times']}): {i['text']}")
    return "\n".join(lines)


def preserve_counts(conn, agent_id: str, before_week: int | None = None) -> dict[str, dict]:
    """{key: {'times': n, 'weeks': [...], 'text': latest}} over accepted reports."""
    sql = ("SELECT week_no, report_json FROM strategy_reports WHERE agent_id=? AND status='accepted' "
           "AND report_json IS NOT NULL")
    params: list = [agent_id]
    if before_week is not None:
        sql += " AND week_no < ?"
        params.append(before_week)
    out: dict[str, dict] = {}
    for row in conn.execute(sql + " ORDER BY week_no", params):
        seen = set()
        for s in json.loads(row["report_json"]).get("slots", []):
            for i in s.get("what_to_preserve", []):
                if i["key"] in seen:
                    continue            # count a key once per week
                seen.add(i["key"])
                e = out.setdefault(i["key"], {"times": 0, "weeks": [], "text": ""})
                e["times"] += 1
                e["weeks"].append(row["week_no"])
                e["text"] = i["text"]
    return out


def briefing_section(conn, agent_id: str, week_no: int) -> list[str]:
    """For the decision briefing: the agent's last reports + recurring lessons."""
    n = SETTINGS.reports_in_briefing
    rows = conn.execute("SELECT week_no, report_text FROM strategy_reports WHERE agent_id=? AND status='accepted' "
                        "AND week_no < ? ORDER BY week_no DESC LIMIT ?", (agent_id, week_no, n)).fetchall()
    if not rows:
        return []
    lines = ["", f"## Your strategy reports (last {len(rows)}, newest first)",
             "Written by you after each scored week; the numbers are the engine's."]
    for r in rows:
        lines += ["", r["report_text"][:3000]]
    counts = {k: v for k, v in preserve_counts(conn, agent_id, week_no).items() if v["times"] > 1}
    if counts:
        lines += ["", "Lessons you keep marking to preserve (times): " + "; ".join(
            f"{k} x{v['times']}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]["times"]))]
    return lines


# --------------------------------------------------------------------------
# The report turn
# --------------------------------------------------------------------------

REPORT_PROMPT = """You are "{agent}" in the AI Trading League. Week {week} has been scored. Write a short report on YOUR OWN strategies for that week, to help you decide better next time.

This is a reporting turn, not a trading turn: you can't trade here. Your tools (MCP server "arena") are read-only and show only your own data:
- get_week_numbers: the engine's numbers per slot (use these; don't compute your own)
- get_week_trades: your trades in that week, with your stated reasons
- get_my_decision: the decision you submitted for that week
- get_past_reports: your previous reports
You have at most {budget} tool calls. Then call submit_report once (one entry per slot). Be concrete and brief. For what_to_preserve, reuse an existing key from your past reports when it is the same lesson.

The engine's numbers for week {week}:
{numbers}

{past_keys}"""


def pending_weeks(conn, simulate: bool = False) -> list[int]:
    """Scored weeks that still need reports: the latest two scored weeks only,
    so turning reports on never replays a long history."""
    rows = conn.execute("SELECT week_no FROM weeks WHERE status='scored' AND simulate=? "
                        "ORDER BY week_no DESC LIMIT 2", (int(simulate),)).fetchall()
    return sorted(r[0] for r in rows)


def run_reports(conn: sqlite3.Connection, factory, simulate: bool = False,
                weeks: list[int] | None = None, force: bool = False) -> list[dict]:
    """Write the missing reports. Returns one summary dict per report attempted."""
    from . import ledger
    from .agents.cli import CliAgent
    done = []
    for week_no in (weeks or pending_weeks(conn, simulate)):
        for agent_id in ledger.agent_ids(conn):
            row = conn.execute("SELECT status FROM strategy_reports WHERE week_no=? AND agent_id=?",
                               (week_no, agent_id)).fetchone()
            if row and row["status"] != "running" and not force:
                continue            # accepted, or failed once already (alerted)
            agent = factory(agent_id)
            if not isinstance(agent, CliAgent):
                done.append({"week": week_no, "agent": agent_id, "status": "skipped",
                             "error": "reports need the CLI backend"})
                continue
            done.append(run_report_turn(conn, agent, week_no, simulate))
    return done


def run_report_turn(conn, agent, week_no: int, simulate: bool = False) -> dict:
    from . import alerts
    from .agents.base import Usage
    from .agents.cli import CliRun, mcp_launch

    agent_id = agent.agent_id
    numbers = week_numbers(conn, agent_id, week_no)
    if numbers is None:
        return {"week": week_no, "agent": agent_id, "status": "skipped", "error": "week not scored"}
    db_file = conn.execute("PRAGMA database_list").fetchone()["file"]
    with transaction(conn):
        conn.execute("INSERT OR REPLACE INTO strategy_reports(week_no, agent_id, status, model, started_at) "
                     "VALUES (?,?,'running',?,?)", (week_no, agent_id, agent.model or "(cli default)", _now()))
    keys = preserve_counts(conn, agent_id, week_no)
    past_keys = ("Keys you used before: " + ", ".join(f"{k} (x{v['times']})" for k, v in keys.items())
                 if keys else "")
    prompt = REPORT_PROMPT.format(agent=agent_id, week=week_no, budget=SETTINGS.report_max_tool_calls,
                                  numbers=numbers_text(numbers), past_keys=past_keys)
    workdir = Path(tempfile.mkdtemp(prefix=f"arena_report_{agent_id}_"))
    result_path = workdir / "result.json"
    mcp = mcp_launch(db_file, agent_id, numbers["end_date"], week_no, 0, result_path, simulate, report=True)
    runs: list[CliRun] = []
    state: dict = {}
    try:
        runs.append(agent.run(prompt, workdir, mcp, web_search=False, timeout=SETTINGS.report_timeout_sec))
        state = _read(result_path)
        if not state.get("accepted") and runs[-1].session_id and "timed out" not in (runs[-1].error or ""):
            runs.append(agent.run("Call submit_report now with your report (one entry per slot).", workdir, mcp,
                                  web_search=False, resume_session=runs[-1].session_id,
                                  timeout=SETTINGS.report_timeout_sec))
            state = _read(result_path)
    except Exception as e:  # noqa: BLE001 - never leave the report 'running'
        runs.append(CliRun(error=f"Report turn crashed: {type(e).__name__}: {e}"))
        state = state or _read(result_path)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    usage = Usage()
    for r in runs:
        usage.add(r.usage)
    accepted = bool(state.get("accepted"))
    report_json = report_text = None
    error = None
    if accepted:
        repeats = {k: v["times"] for k, v in keys.items()}
        for s in state["report"].get("slots", []):
            for i in s.get("what_to_preserve", []):
                repeats[i["key"]] = keys.get(i["key"], {"times": 0})["times"] + 1
        merged = merge(state["report"], numbers, repeats)
        report_json, report_text = json.dumps(merged), render_text(merged)
    else:
        error = (runs[-1].error if runs and runs[-1].error else
                 "; ".join(state.get("last_errors", [])) or "The agent didn't submit a report.")[:500]
    with transaction(conn):
        conn.execute("UPDATE strategy_reports SET status=?, report_json=?, report_text=?, error=?, tool_calls=?, "
                     "web_searches=?, input_tokens=?, cached_tokens=?, cache_write_tokens=?, output_tokens=?, "
                     "finished_at=? WHERE week_no=? AND agent_id=?",
                     ("accepted" if accepted else "failed", report_json, report_text, error,
                      state.get("tool_calls", 0), sum(r.web_searches for r in runs), usage.input, usage.cached,
                      usage.cache_write, usage.output, _now(), week_no, agent_id))
        log_event(conn, "info" if accepted else "error", f"report:{agent_id}",
                  f"week {week_no}: report {'accepted' if accepted else 'failed'}{' - ' + error if error else ''}")
    if not accepted and not simulate:
        alerts.send(f"Strategy report FAILED ({agent_id}, week {week_no}): {error}", level="warning")
    return {"week": week_no, "agent": agent_id, "status": "accepted" if accepted else "failed", "error": error}


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
