"""A tiny MCP server that gives a CLI agent (Claude Code or Codex) its tools
for one weekly turn.

MCP over stdio is just JSON-RPC 2.0: one JSON message per line on stdin,
one JSON reply per line on stdout. We only need four methods:
initialize, tools/list, tools/call and ping. Writing them by hand keeps
this file small and dependency-free.

The CLI starts this process itself (see agents/cli.py), one per turn:
    python -m arena.mcp_server --db ... --agent claude --date 2024-06-28 \
        --week 3 --turn-id 7 --result C:/tmp/result.json [--simulate]

The league reads the outcome from the --result JSON file after the CLI exits.

With --http it serves the same protocol as a "streamable HTTP" MCP server
on 127.0.0.1 instead of stdio, and prints "PORT <n>" once it listens. The
league uses this for an agent that runs inside the Landlock jail
(agents/jail.py): this server then runs OUTSIDE the jail, so the agent's
process can't write to the database or the result file, while the tools can.
Every request must carry "Authorization: Bearer <ARENA_MCP_TOKEN>".

With --report the server is a ReportServer instead: the weekly strategy
report turn (arena/reports.py). It opens the database READ-ONLY and its
tools only ever query the agent's own rows.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from pathlib import Path

from .agents.decision import SUBMIT_SPEC, check, targets_to_json
from .config import SETTINGS
from .db import connect
from .tools.context import ResearchContext
from .tools.registry import TOOL_SPECS, Budget, ToolBox

log = logging.getLogger("arena.mcp")
SERVER_INFO = {"name": "arena", "version": "1.0"}
PING_SPEC = {"name": "ping", "description": "Connectivity check. Returns 'pong'.",
             "parameters": {"type": "object", "properties": {}}}


class TurnServer:
    """Tool handlers + the turn's state (budget, submit attempts, outcome)."""

    def __init__(self, args):
        self.args = args
        self.result_path = Path(args.result)
        self.state = {"attempts": 0, "accepted": False, "decision": None, "targets": None,
                      "slot_runs": [], "last_errors": [], "tool_calls": 0, "pinged": False}
        if args.probe:
            self.specs = [PING_SPEC]
            return
        self.conn = connect(args.db)
        self.ctx = ResearchContext(self.conn, args.agent, args.date, simulate=args.simulate)
        # API spend isn't metered here (subscription CLIs); tool calls are.
        self.toolbox = ToolBox(self.ctx, Budget(max_calls=SETTINGS.max_tool_calls, max_spend_usd=float("inf")))
        self.specs = TOOL_SPECS + [SUBMIT_SPEC]
        self._save()

    def _save(self) -> None:
        tmp = self.result_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state), encoding="utf-8")
        tmp.replace(self.result_path)   # atomic: the league never reads half a file

    def list_tools(self) -> list[dict]:
        return [{"name": s["name"], "description": s["description"], "inputSchema": s["parameters"]}
                for s in self.specs]

    def call(self, name: str, arguments: dict) -> tuple[str, bool]:
        """Returns (text, is_error)."""
        if self.args.probe:
            if name != "ping":
                return f"Unknown tool {name!r}", True
            self.state["pinged"] = True
            self._save()
            return "pong", False
        if name == SUBMIT_SPEC["name"]:
            return self._submit(arguments)
        self.state["tool_calls"] += 1
        text = self.toolbox.call(name, arguments)
        self._save()
        return text, text.startswith('{"error"')

    def _submit(self, decision: dict) -> tuple[str, bool]:
        s = self.state
        if s["accepted"]:
            return "Ignored: a decision was already accepted this week.", True
        max_attempts = SETTINGS.max_decision_retries + 1
        if s["attempts"] >= max_attempts:
            return "No retries left: your portfolio stays unchanged this week.", True
        s["attempts"] += 1
        checked = check(self.ctx, decision)
        self.conn.execute(
            "INSERT INTO decision_attempts(turn_id, attempt, ok, decision_json, errors) VALUES (?,?,?,?,?)",
            (self.args.turn_id, s["attempts"], int(checked.ok), json.dumps(decision), json.dumps(checked.errors)))
        s["slot_runs"] = checked.slot_runs
        s["last_errors"] = checked.errors
        if checked.ok:
            s.update(accepted=True, decision=decision, targets=targets_to_json(checked.targets))
            self._save()
            return checked.message + "\nYour turn is complete; you can stop now.", False
        self._save()
        left = max_attempts - s["attempts"]
        tail = (f"\n\nFix the problems and call submit_decision again ({left} "
                f"retr{'y' if left == 1 else 'ies'} left)." if left else
                "\n\nNo retries left: your portfolio stays unchanged this week.")
        return checked.message + tail, True

    def close(self) -> None:
        if not self.args.probe:
            self.ctx.close_session()
            self.conn.close()


REPORT_TOOL_SPECS = [
    {"name": "get_week_numbers", "description": "The engine's numbers for the scored week: per slot return, "
     "P&L, contribution, biggest winner/loser, costs, hit rate, and P&L per ticker.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_week_trades", "description": "Your trades in that week (including the opening rebalance), "
     "with the reason you gave for each.", "parameters": {"type": "object", "properties": {}}},
    {"name": "get_my_decision", "description": "The decision you submitted for that week.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_past_reports", "description": "Your previous weekly reports (newest first).",
     "parameters": {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 4}}}},
]


class ReportServer:
    """Tools for the weekly report turn: read-only, own data only, small budget."""

    def __init__(self, args):
        import sqlite3

        from . import reports
        self.args, self.reports = args, reports
        self.result_path = Path(args.result)
        self.state = {"accepted": False, "report": None, "attempts": 0, "last_errors": [], "tool_calls": 0}
        self.conn = sqlite3.connect(f"file:{Path(args.db).resolve().as_posix()}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row
        self.agent, self.week = args.agent, args.week
        self.numbers = reports.week_numbers(self.conn, self.agent, self.week)
        self.specs = REPORT_TOOL_SPECS + [reports.SUBMIT_REPORT_SPEC]
        self._save()

    _save = TurnServer._save
    list_tools = TurnServer.list_tools

    def call(self, name: str, arguments: dict) -> tuple[str, bool]:
        if name == "submit_report":
            return self._submit(arguments)
        if self.state["tool_calls"] >= SETTINGS.report_max_tool_calls:
            return "Tool budget used up: call submit_report now.", True
        self.state["tool_calls"] += 1
        self._save()
        a, w = self.agent, self.week
        if name == "get_week_numbers":
            return json.dumps(self.numbers), False
        if name == "get_week_trades":
            win = self.reports.week_window(self.conn, a, w)
            if win is None:
                return "[]", False
            rows = self.conn.execute(
                "SELECT date, slot, ticker, shares, fill_price, value, commission, slippage_cost, reason, note "
                "FROM trades WHERE agent_id=? AND seq>? AND seq<=? ORDER BY seq",
                (a, win["cost_from_seq"], win["s1"]["seq"])).fetchall()
            return json.dumps([dict(r) for r in rows]), False
        if name == "get_my_decision":
            r = self.conn.execute("SELECT decision_json FROM agent_turns WHERE week_no=? AND agent_id=? "
                                  "AND status='accepted'", (w, a)).fetchone()
            return (r["decision_json"] if r and r["decision_json"] else "No accepted decision that week."), False
        if name == "get_past_reports":
            n = max(1, min(4, int(arguments.get("n", 4))))
            rows = self.conn.execute("SELECT report_text FROM strategy_reports WHERE agent_id=? AND week_no<? "
                                     "AND status='accepted' ORDER BY week_no DESC LIMIT ?", (a, w, n)).fetchall()
            return ("\n\n".join(r["report_text"] for r in rows) or "No earlier reports."), False
        return f"Unknown tool {name!r}", True

    def _submit(self, report: dict) -> tuple[str, bool]:
        s = self.state
        if s["accepted"]:
            return "Ignored: your report was already accepted.", True
        if s["attempts"] >= 3:
            return "No retries left.", True
        s["attempts"] += 1
        errors = self.reports.check_report(report, self.numbers) if self.numbers else ["The week isn't scored."]
        s["last_errors"] = errors
        if errors:
            self._save()
            return "Report REJECTED:\n- " + "\n- ".join(errors) + "\nFix it and call submit_report again.", True
        s.update(accepted=True, report=report)
        self._save()
        return "Report ACCEPTED. You're done; you can stop now.", False

    def close(self) -> None:
        self.conn.close()


def handle(server: TurnServer, msg: dict) -> dict | None:
    """One JSON-RPC message in, one reply out (None for notifications)."""
    method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if msg_id is None:          # notification (e.g. notifications/initialized): no reply
        return None
    try:
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                      "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO}
        elif method == "tools/list":
            result = {"tools": server.list_tools()}
        elif method == "tools/call":
            text, is_error = server.call(params.get("name", ""), params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": text}], "isError": is_error}
        elif method == "ping":
            result = {}
        else:
            return {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"}}
    except Exception as e:  # noqa: BLE001 - a tool bug must not kill the agent's turn
        log.error("tool failure: %s", traceback.format_exc())
        result = {"content": [{"type": "text", "text": f"Tool failed: {type(e).__name__}: {e}"}],
                  "isError": True}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def serve_http(server, token: str, out, host: str = "127.0.0.1") -> None:
    """MCP over HTTP: POST /mcp with a JSON-RPC message (or a batch); JSON
    reply, or 202 for notifications. No server-sent events are needed."""
    import hmac
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    lock = threading.Lock()          # the tool state isn't thread-safe

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code: int, body: bytes = b"", ctype: str = "application/json") -> None:
            self.send_response(code)
            if body:
                self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _authorized(self) -> bool:
            got = self.headers.get("Authorization", "")
            return hmac.compare_digest(got.encode(), f"Bearer {token}".encode())

        def do_POST(self):
            if self.path.split("?")[0].rstrip("/") != "/mcp":
                return self._send(404)
            if not self._authorized():
                return self._send(401)
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                return self._send(400, json.dumps({"jsonrpc": "2.0", "id": None,
                                                   "error": {"code": -32700, "message": "Parse error"}}).encode())
            batch = isinstance(msg, list)
            with lock:
                replies = [r for r in (handle(server, m) for m in (msg if batch else [msg])) if r is not None]
            if not replies:
                return self._send(202)
            self._send(200, json.dumps(replies if batch else replies[0]).encode())

        def do_GET(self):
            self._send(405)          # no server-initiated messages

        def do_DELETE(self):
            self._send(200 if self._authorized() else 401)

    httpd = HTTPServer((host, 0), Handler)
    out.write(f"PORT {httpd.server_address[1]}\n")
    out.flush()
    httpd.serve_forever()


def serve(server: TurnServer, stdin, stdout) -> None:
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            reply = handle(server, msg)
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db")
    p.add_argument("--agent")
    p.add_argument("--date")
    p.add_argument("--week", type=int)
    p.add_argument("--turn-id", type=int)
    p.add_argument("--result", required=True)
    p.add_argument("--simulate", action="store_true")
    p.add_argument("--probe", action="store_true", help="expose only a 'ping' tool (check-agents)")
    p.add_argument("--report", action="store_true", help="the weekly report turn (read-only tools)")
    p.add_argument("--http", action="store_true", help="serve on 127.0.0.1 over HTTP (token in ARENA_MCP_TOKEN)")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    # stdout carries the protocol; anything else printed by accident would
    # corrupt it, so send stray prints to stderr.
    protocol_out = sys.stdout
    sys.stdout = sys.stderr
    server = ReportServer(args) if args.report else TurnServer(args)
    try:
        if args.http:
            import os
            token = os.environ.get("ARENA_MCP_TOKEN", "")
            if len(token) < 20:
                sys.exit("--http needs ARENA_MCP_TOKEN (at least 20 characters)")
            import signal
            # The league stops us with SIGTERM: exit through `finally` so the
            # sandbox's temporary price files are cleaned up.
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
            serve_http(server, token, protocol_out)
        else:
            serve(server, sys.stdin, protocol_out)
    finally:
        server.close()


if __name__ == "__main__":
    main()
