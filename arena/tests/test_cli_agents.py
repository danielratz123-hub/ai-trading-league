"""Subscription (CLI) backend: the MCP server, the CLI turn flow, the
lockdown of the Claude Code / Codex commands, and execution at the open."""
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pandas as pd
import pytest

from arena import league, ledger, prices, risk
from arena.agents import cli
from arena.agents.cli import ClaudeCodeAgent, CliAgent, CodexAgent, McpLaunch, mcp_launch
from arena.config import PROJECT_ROOT
from arena.db import connect
from arena.ledger import SlotTarget
from arena.trading_calendar import store_days

from .fakes import manual, submit
from .test_tools import _market

FAKE_CLI = Path(__file__).with_name("fake_cli.py")
WEEK1 = "2024-01-05"


@pytest.fixture
def db_file(tmp_path):
    path = tmp_path / "arena.db"
    c = connect(path)
    df = _market()
    c.executemany("INSERT INTO prices VALUES (?,?,?,?,?,?,?)", df.itertuples(index=False))
    store_days(c, [(d, "09:30", "16:00") for d in sorted(df["date"].unique())])
    yield path, c
    c.close()


# ---------------------------------------------------------------- MCP server


def rpc_session(launch: McpLaunch, messages: list[dict]) -> list[dict]:
    p = subprocess.run([launch.command, *launch.args], input="\n".join(json.dumps(m) for m in messages) + "\n",
                       capture_output=True, text=True, env={**os.environ, **launch.env}, timeout=120)
    return [json.loads(line) for line in p.stdout.splitlines() if line.strip()]


def test_mcp_server_speaks_json_rpc_and_validates_submissions(db_file, tmp_path):
    path, conn = db_file
    ledger.create_agent(conn, "claude", 1000, WEEK1)
    result = tmp_path / "result.json"
    launch = mcp_launch(path, "claude", WEEK1, 1, 99, result, simulate=False)
    bad = {"market_view": "v", "notes_to_self": "n", "slots": [manual(1, 1.0, {"NVDA": 0.9})]}
    good = {"market_view": "v", "notes_to_self": "n", "slots": [manual(1, 0.5, {"SPY": 0.5})]}
    replies = rpc_session(launch, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "get_indicators", "arguments": {"tickers": ["AAPL"], "indicators": ["rsi_14"]}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "submit_decision", "arguments": bad}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "submit_decision", "arguments": good}},
        {"jsonrpc": "2.0", "id": 6, "method": "bogus/method"},
    ])
    by_id = {r["id"]: r for r in replies}
    assert len(replies) == 6                                     # no reply to the notification
    assert by_id[1]["result"]["protocolVersion"] == "2025-06-18"
    names = {t["name"] for t in by_id[2]["result"]["tools"]}
    assert {"screen", "backtest", "run_python", "submit_decision"} <= names
    assert "rsi_14" in by_id[3]["result"]["content"][0]["text"]
    assert by_id[4]["result"]["isError"] and "NVDA" in by_id[4]["result"]["content"][0]["text"]
    assert not by_id[5]["result"]["isError"] and "ACCEPTED" in by_id[5]["result"]["content"][0]["text"]
    assert by_id[6]["error"]["code"] == -32601
    state = json.loads(result.read_text())
    assert state["accepted"] and state["attempts"] == 2 and state["tool_calls"] == 1
    assert conn.execute("SELECT COUNT(*) FROM decision_attempts WHERE turn_id=99").fetchone()[0] == 2


# ---------------------------------------------------------------- CLI turn flow


class FakeCli(CliAgent):
    """Runs fake_cli.py; `runs` is a list of tool-call lists, one per CLI run
    (the 2nd run happens only if the league nudges via resume)."""

    def __init__(self, agent_id, runs):
        super().__init__(agent_id, "fake")
        self.runs, self.run_no, self.resumed = runs, 0, []

    def command(self, workdir, mcp, web_search, resume_session):
        self.resumed.append(resume_session)
        plan = workdir / "plan.json"
        plan.write_text(json.dumps({"runs": self.runs, "run_no": self.run_no, "env": dict(os.environ),
                                    "mcp": {"command": mcp.command, "args": mcp.args, "env": mcp.env}}))
        self.run_no += 1
        return [sys.executable, str(FAKE_CLI), str(plan)]

    def parse_event(self, ev, run):
        if ev.get("type") in ("system", "result"):
            run.session_id = ev.get("session_id")


def test_cli_turn_end_to_end(db_file):
    path, conn = db_file
    claude = FakeCli("claude", [[["market_overview", {}], list(submit([manual(1, 1.0, {"AAPL": 0.25})]))]])
    gpt = FakeCli("gpt", [[list(submit([]))]])
    reports = league.decide(conn, WEEK1, factory=lambda a: {"claude": claude, "gpt": gpt}[a])
    assert [r.status for r in reports] == ["accepted", "accepted"]
    turn = conn.execute("SELECT * FROM agent_turns WHERE agent_id='claude'").fetchone()
    assert turn["tool_calls"] == 1 and turn["attempts"] == 1
    assert conn.execute("SELECT COUNT(*) FROM turn_messages WHERE turn_id=?", (turn["id"],)).fetchone()[0] > 1
    league.execute(conn)
    assert {p["ticker"] for p in ledger.positions(conn, "claude")} == {"AAPL"}


def test_cli_agent_that_forgets_to_submit_is_resumed_once_with_a_nudge(db_file):
    path, conn = db_file
    claude = FakeCli("claude", [[["market_overview", {}]], [list(submit([]))]])
    gpt = FakeCli("gpt", [[list(submit([]))]])
    reports = league.decide(conn, WEEK1, factory=lambda a: {"claude": claude, "gpt": gpt}[a])
    assert reports[0].status == "accepted"
    assert claude.resumed == [None, "sess-1"]          # 2nd run resumed the same session


def test_cli_agent_that_never_submits_fails_and_keeps_portfolio(db_file):
    path, conn = db_file
    claude = FakeCli("claude", [[["market_overview", {}]], [["market_overview", {}]], []])
    gpt = FakeCli("gpt", [[list(submit([]))]])
    reports = league.decide(conn, WEEK1, factory=lambda a: {"claude": claude, "gpt": gpt}[a])
    assert reports[0].status == "failed" and "without calling submit_decision" in reports[0].error
    assert conn.execute("SELECT COUNT(*) FROM pending_orders WHERE agent_id='claude'").fetchone()[0] == 0


def test_cli_backend_needs_a_database_file():
    c = connect(":memory:")
    df = _market()
    c.executemany("INSERT INTO prices VALUES (?,?,?,?,?,?,?)", df.itertuples(index=False))
    store_days(c, [(d, "09:30", "16:00") for d in sorted(df["date"].unique())])
    with pytest.raises(league.LeagueError, match="database file"):
        league.decide(c, WEEK1, factory=lambda a: FakeCli(a, []))


# ---------------------------------------------------------------- lockdown


def launch():
    return mcp_launch("C:/x/arena.db", "claude", WEEK1, 1, 1, "C:/x/result.json", simulate=False)


def test_claude_code_command_is_locked_down(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "resolve_cli", lambda name, js: [name])
    argv = ClaudeCodeAgent("claude", "claude-opus-5-5").command(tmp_path, launch(), True, None)
    assert argv[argv.index("--tools") + 1] == "WebSearch"           # no Bash / Read / Edit
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5"
    cfg = json.loads((tmp_path / "mcp.json").read_text())
    assert list(cfg["mcpServers"]) == ["arena"]
    sim = ClaudeCodeAgent("claude", "").command(tmp_path, launch(), False, "sess")
    assert sim[sim.index("--tools") + 1] == "" and "WebSearch" not in sim
    assert sim[sim.index("--resume") + 1] == "sess" and "--model" not in sim


def merge(into: dict, new: dict) -> None:
    for k, v in new.items():
        if isinstance(v, dict) and isinstance(into.get(k), dict):
            merge(into[k], v)
        else:
            into[k] = v


def test_codex_command_is_locked_down_and_valid_toml(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "resolve_cli", lambda name, js: [name])
    argv = CodexAgent("gpt", "").command(tmp_path, launch(), True, None)
    assert argv[:2] == ["codex", "exec"] and argv[-1] == "-" and "-m" not in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only" and "--ignore-user-config" in argv
    overrides = {}
    for i, a in enumerate(argv):
        if a == "-c":
            merge(overrides, tomllib.loads(argv[i + 1]))   # every override must be valid TOML
    features = overrides["features"]
    for f in ("shell_tool", "unified_exec", "apps", "multi_agent", "image_generation", "browser_use"):
        assert features[f] is False, f                # no shell, connectors, sub-agents...
    assert features.get("code_mode_host", True) is not False   # code mode is how Codex calls ANY tool
    assert overrides["agents"]["enabled"] is False
    assert overrides["mcp_servers"]["arena"]["default_tools_approval_mode"] == "approve"
    assert overrides["approval_policy"] == "never" and overrides["web_search"] == "live"
    server = overrides["mcp_servers"]["arena"]
    assert server["args"][:2] == ["-m", "arena.mcp_server"]
    assert server["env"]["PYTHONPATH"] == str(PROJECT_ROOT)
    off = CodexAgent("gpt", "gpt-x").command(tmp_path, launch(), False, "thread-1")
    assert any(a == 'web_search="disabled"' for a in off)
    assert off[-3:] == ["resume", "thread-1", "-"] and off[off.index("-m") + 1] == "gpt-x"


def test_cli_env_has_no_api_keys(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ALPACA_SECRET_KEY"):
        monkeypatch.setenv(k, "secret")
    for agent in (ClaudeCodeAgent("claude", ""), CodexAgent("gpt", "")):
        env = agent.env()
        assert not {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ALPACA_SECRET_KEY"} & set(env)


def test_event_parsing_counts_searches_and_shell_use():
    run = cli.CliRun()
    c = ClaudeCodeAgent("claude", "")
    c.parse_event({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "WebSearch"}, {"type": "tool_use", "name": "mcp__arena__screen"}]}}, run)
    c.parse_event({"type": "result", "session_id": "s", "usage": {"input_tokens": 5, "output_tokens": 7},
                   "total_cost_usd": 0.12, "result": "ok"}, run)
    assert run.web_searches == 1 and run.session_id == "s" and run.usage.output == 7

    run = cli.CliRun()
    g = CodexAgent("gpt", "")
    for ev in [{"type": "thread.started", "thread_id": "t1"},
               {"type": "item.completed", "item": {"type": "web_search"}},
               {"type": "item.completed", "item": {"type": "command_execution"}},
               {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40,
                                                    "output_tokens": 9}}]:
        g.parse_event(ev, run)
    assert run.session_id == "t1" and run.web_searches == 1 and run.shell_commands == 1
    assert run.usage.input == 60 and run.usage.cached == 40


# ---------------------------------------------------------------- execution at the open


def test_orders_fill_at_the_captured_opening_price(db_file):
    path, conn = db_file
    d = "2024-04-01"
    conn.execute("DELETE FROM prices WHERE date >= ?", (d,))       # it's the morning: no daily bar yet
    ledger.create_agent(conn, "claude", 1000, "2024-03-28")
    conn.execute("INSERT INTO weeks(week_no, decision_date, exec_date, status) VALUES (1, '2024-03-28', ?, 'decided')", (d,))
    conn.execute("INSERT INTO pending_orders(agent_id, created_date, exec_date, kind, payload) VALUES "
                 "('claude', '2024-03-28', ?, 'rebalance', ?)",
                 (d, json.dumps([SlotTarget(1, 1.0, {"MSFT": 0.25}).__dict__])))
    assert set(league.tickers_needed_at_open(conn, d)) >= {"SPY", "MSFT"}
    with pytest.raises(league.LeagueError, match="opening prices"):
        league.run_open(conn, d)
    prices.store_opening_prices(conn, pd.DataFrame({"ticker": ["SPY", "MSFT"], "open": [500.0, 400.0]}),
                                d, "test")
    rep = league.run_open(conn, d)
    assert rep.weekly is not None and rep.weekly.trades["claude"] == 1
    t = conn.execute("SELECT * FROM trades").fetchone()
    assert t["ref_price"] == 400.0 and t["date"] == d


def test_stop_orders_fill_at_next_open_on_non_execution_days(db_file):
    path, conn = db_file
    ledger.create_agent(conn, "claude", 1000, "2024-01-02")
    ledger.rebalance(conn, "claude", "2024-01-02", [SlotTarget(1, 0.5, {"AAPL": 0.5})])
    conn.execute("INSERT INTO pending_orders(agent_id, created_date, exec_date, kind, slot, ticker) "
                 "VALUES ('claude', '2024-01-02', '2024-01-03', 'stop_loss', 1, 'AAPL')")
    rep = league.run_open(conn, "2024-01-03")
    assert rep.weekly is None and rep.risk_fills == {"claude": 1}
    assert ledger.position_shares(conn, "claude", 1, "AAPL") == 0


def test_model_catalog_live_and_fallback(monkeypatch):
    from arena.agents import models
    models._cache.clear()

    class Done:
        stdout = ('{"models": [{"slug": "gpt-new", "display_name": "GPT-New", "description": "d", "visibility": "list"},'
                  ' {"slug": "secret", "visibility": "hide"}]}')
    monkeypatch.setattr(models, "resolve_cli", lambda *a: ["codex"])
    monkeypatch.setattr(models.subprocess, "run", lambda *a, **k: Done())
    c = models.catalog()
    assert c["gpt_source"] == "live" and [m["id"] for m in c["gpt"]] == ["gpt-new"]   # hidden ones dropped
    assert any(m["id"] == "claude-opus-5-5" for m in c["claude"])

    models._cache.clear()
    def boom(*a, **k):
        raise FileNotFoundError("no codex")
    monkeypatch.setattr(models, "resolve_cli", boom)
    c = models.catalog()
    assert c["gpt_source"] == "built-in" and c["gpt"] == models.GPT_FALLBACK
    models._cache.clear()
