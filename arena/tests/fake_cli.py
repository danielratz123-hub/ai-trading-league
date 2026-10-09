"""A stand-in for `claude -p` / `codex exec` used by the tests.

It reads a plan file, starts the real arena MCP server exactly like a CLI
would, makes the planned tool calls over MCP, and prints JSON events.

    python fake_cli.py plan.json            (prompt arrives on stdin)
"""
import json
import subprocess
import sys


def main() -> None:
    plan = json.load(open(sys.argv[1], encoding="utf-8"))
    sys.stdin.read()                                    # the prompt (ignored)
    run_no = plan["run_no"]
    steps = plan["runs"][run_no] if run_no < len(plan["runs"]) else []
    mcp = plan["mcp"]
    server = subprocess.Popen([mcp["command"], *mcp["args"]], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, env={**plan["env"], **mcp["env"]}, text=True)

    def rpc(i, method, params=None):
        server.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method,
                                       "params": params or {}}) + "\n")
        server.stdin.flush()
        return json.loads(server.stdout.readline())

    print(json.dumps({"type": "system", "session_id": "sess-1"}))
    rpc(1, "initialize", {"protocolVersion": "2025-06-18"})
    server.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    tools = [t["name"] for t in rpc(2, "tools/list")["result"]["tools"]]
    print(json.dumps({"type": "tools", "names": tools}))
    for n, (name, args) in enumerate(steps):
        reply = rpc(10 + n, "tools/call", {"name": name, "arguments": args})
        print(json.dumps({"type": "tool_result", "name": name, "result": reply["result"]}))
    server.stdin.close()
    server.wait(timeout=60)
    print(json.dumps({"type": "result", "session_id": "sess-1", "usage": {"input_tokens": 10,
                      "output_tokens": 5}, "result": "done"}))


if __name__ == "__main__":
    main()
