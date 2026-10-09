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
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    # stdout carries the protocol; anything else printed by accident would
    # corrupt it, so send stray prints to stderr.
    protocol_out = sys.stdout
    sys.stdout = sys.stderr
    server = TurnServer(args)
    try:
        serve(server, sys.stdin, protocol_out)
    finally:
        server.close()


if __name__ == "__main__":
    main()
