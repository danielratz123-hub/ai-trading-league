"""Agents that run through the official command-line tools, so they use your
Claude and ChatGPT *subscriptions* instead of paid API keys:

    Claude Code   claude -p ...       (sign in once: claude auth login)
    Codex         codex exec ...      (sign in once: codex login)

Each weekly turn starts the CLI once, pointed at our MCP server
(arena/mcp_server.py), which provides the research tools and submit_decision.
The CLIs are locked down so the model can only use those tools plus the
provider's own web search: no shell, no file access, no other MCP servers.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from ..config import PROJECT_ROOT, SETTINGS
from .base import Usage

# Never hand secrets to the CLI process. Removing the provider API keys also
# makes sure each CLI uses the subscription login, not pay-per-token billing.
# Besides these names, anything that LOOKS like a secret (…KEY, …SECRET,
# …TOKEN, …PASSWORD…) is removed too, so a new secret added to .env later
# (mirror keys, Telegram, backups, the dashboard password) is never passed
# on by accident. Each CLI gets back only its own login token (ALLOW_ENV).
SECRET_ENV = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ALPACA_API_KEY", "ALPACA_SECRET_KEY",
              "CODEX_API_KEY")
_SECRET_NAME = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASSWD|HASH|CREDENTIAL)", re.I)


@dataclass
class McpLaunch:
    """How the CLI should start our MCP server."""
    command: str
    args: list[str]
    env: dict[str, str]


@dataclass
class CliRun:
    events: list[dict] = field(default_factory=list)
    session_id: str | None = None
    usage: Usage = field(default_factory=Usage)
    web_searches: int = 0
    shell_commands: int = 0          # must stay 0; recorded as a security check
    error: str | None = None
    api_equivalent_usd: float = 0.0  # what it would cost on the API (info only)
    final_text: str = ""


def mcp_launch(db_path: str, agent_id: str, decision_date: str, week_no: int, turn_id: int,
               result_path: str, simulate: bool, probe: bool = False) -> McpLaunch:
    args = ["-m", "arena.mcp_server", "--db", str(db_path), "--agent", agent_id,
            "--date", decision_date, "--week", str(week_no), "--turn-id", str(turn_id),
            "--result", str(result_path)]
    if simulate:
        args.append("--simulate")
    if probe:
        args.append("--probe")
    return McpLaunch(sys.executable, args, {"PYTHONPATH": str(PROJECT_ROOT), "PYTHONIOENCODING": "utf-8"})


def resolve_cli(name: str, npm_js: list[str]) -> list[str]:
    """Command prefix for a CLI. On Windows, npm installs a .cmd wrapper that
    runs through cmd.exe, which mangles quotes; when we can find the package's
    JavaScript entry point we call node directly instead."""
    exe = shutil.which(name)
    if exe is None:
        raise FileNotFoundError(f"`{name}` was not found on PATH.")
    if exe.lower().endswith((".cmd", ".bat")):
        base = Path(exe).parent / "node_modules"
        node = shutil.which("node")
        for rel in npm_js:
            js = base / rel
            if js.exists() and node:
                return [node, str(js)]
    return [exe]


def _clean_env(extra: dict[str, str] | None = None, allow: tuple[str, ...] = ()) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k in allow or (k not in SECRET_ENV and not _SECRET_NAME.search(k))}
    env.update(extra or {})
    return env


# What a CLI prints when its sign-in is missing, expired or revoked. Used to
# tell "the login is broken" (retry after you fix it) from "the agent made a
# bad decision" (the turn counts as failed).
_LOGIN_ERROR = re.compile(
    r"(not (logged|signed) in|please run /login|run `?(claude|codex) login|invalid api key|"
    r"oauth token (has )?(expired|revoked)|authentication[_ ](error|failed)|unauthori[sz]ed|\b401\b|"
    r"refresh token|login required|token (is )?expired)", re.I)


def looks_like_login_error(text: str | None) -> bool:
    return bool(text) and bool(_LOGIN_ERROR.search(text))


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        os.killpg(proc.pid, signal.SIGKILL)


class CliAgent(ABC):
    """One provider's CLI. Stateless between turns; a turn may be resumed once."""

    NPM_JS: list[str] = []
    CLI_NAME = ""
    LOGIN_CMD: list[str] = []
    INSTALL_HINT = ""
    LOGIN_HINT = ""

    def __init__(self, agent_id: str, model: str):
        self.agent_id = agent_id
        self.model = model

    def _quick(self, args: list[str]) -> tuple[int, str]:
        try:
            p = subprocess.run(resolve_cli(self.CLI_NAME, self.NPM_JS) + args, capture_output=True,
                               timeout=60, env=self.env(),
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except FileNotFoundError as e:
            return 127, str(e)
        except subprocess.TimeoutExpired:
            return 124, "timed out"
        return p.returncode, (p.stdout or p.stderr).decode("utf-8", errors="replace").strip()

    def check_install(self) -> tuple[bool, str]:
        code, text = self._quick(["--version"])
        return code == 0, text.splitlines()[0] if text else f"exit code {code}"

    def check_login(self) -> tuple[bool, str]:
        code, text = self._quick(self.LOGIN_CMD)
        return code == 0, " ".join(text.split())[:200] or f"exit code {code}"

    @abstractmethod
    def command(self, workdir: Path, mcp: McpLaunch, web_search: bool,
                resume_session: str | None) -> list[str]:
        """Full argv; the prompt is sent on stdin."""

    @abstractmethod
    def parse_event(self, ev: dict, run: CliRun) -> None:
        """Fold one JSON event from the CLI's output into `run`."""

    def env(self) -> dict[str, str]:
        return _clean_env()

    def run(self, prompt: str, workdir: Path, mcp: McpLaunch, web_search: bool,
            resume_session: str | None = None, timeout: int | None = None) -> CliRun:
        run = CliRun()
        timeout = timeout or SETTINGS.cli_turn_timeout_sec
        try:
            argv = self.command(workdir, mcp, web_search, resume_session)
        except FileNotFoundError as e:
            run.error = str(e)
            return run
        try:
            proc = subprocess.Popen(
                argv, cwd=workdir, env=self.env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, start_new_session=(os.name != "nt"),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except OSError as e:   # e.g. node missing or a broken install
            run.error = f"Could not start {type(self).__name__}: {e}"
            return run
        try:
            out, err = proc.communicate(prompt.encode("utf-8"), timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            out, err = proc.communicate()
            run.error = f"Turn timed out after {timeout}s."
        for line in out.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            run.events.append(ev)
            self.parse_event(ev, run)
        if proc.returncode not in (0, None) and run.error is None:
            tail = err.decode("utf-8", errors="replace")[-1500:]
            run.error = f"{type(self).__name__} exited with code {proc.returncode}: {tail}"
        return run


# --------------------------------------------------------------------------
# Claude Code
# --------------------------------------------------------------------------

class ClaudeCodeAgent(CliAgent):
    NPM_JS = ["@anthropic-ai/claude-code/cli.js"]
    CLI_NAME = "claude"
    LOGIN_CMD = ["auth", "status"]
    INSTALL_HINT = "Install or update: npm install -g @anthropic-ai/claude-code  (or: claude update)"
    LOGIN_HINT = ("Sign in with your Claude subscription: claude auth login "
                  "(on a server: claude setup-token, then CLAUDE_CODE_OAUTH_TOKEN in .env)")

    def check_login(self):
        code, text = self._quick(self.LOGIN_CMD)
        if code != 0:
            return False, " ".join(text.split())[:200] or f"exit code {code}"
        try:
            method = json.loads(text).get("authMethod")
        except json.JSONDecodeError:
            return True, " ".join(text.split())[:200]
        if method not in ("claude.ai", "oauth_token"):
            return False, f"signed in via {method!r}, not a Claude subscription"
        return True, f"subscription login ({method})"

    def env(self):
        # Backtests can take a while; give MCP tools 5 minutes.
        # CLAUDE_CODE_OAUTH_TOKEN: the long-lived subscription token from
        # `claude setup-token` (headless servers). Only Claude Code gets it.
        return _clean_env({"MCP_TOOL_TIMEOUT": "300000", "MCP_TIMEOUT": "60000"},
                          allow=("CLAUDE_CODE_OAUTH_TOKEN",))

    def command(self, workdir, mcp, web_search, resume_session):
        cfg = workdir / "mcp.json"
        cfg.write_text(json.dumps({"mcpServers": {"arena": {
            "type": "stdio", "command": mcp.command, "args": mcp.args, "env": mcp.env}}}), encoding="utf-8")
        allowed = ["mcp__arena__*"] + (["WebSearch"] if web_search else [])
        argv = resolve_cli("claude", self.NPM_JS) + [
            "-p", "--output-format", "stream-json", "--verbose",
            "--mcp-config", str(cfg), "--strict-mcp-config",       # only our MCP server
            "--tools", "WebSearch" if web_search else "",            # no Bash/Read/Edit/...
            "--allowedTools", *allowed,
            "--permission-mode", "dontAsk",                          # anything else is denied
            "--disable-slash-commands",
            "--max-turns", str(SETTINGS.cli_max_turns),
            "--effort", SETTINGS.reasoning_effort,
        ]
        if self.model:
            argv += ["--model", self.model]
        if resume_session:
            argv += ["--resume", resume_session]
        return argv

    def parse_event(self, ev, run):
        kind = ev.get("type")
        if kind == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") in ("tool_use", "server_tool_use"):
                    name = block.get("name", "")
                    if name in ("WebSearch", "web_search"):
                        run.web_searches += 1
                    elif name.startswith(("Bash", "PowerShell")):
                        run.shell_commands += 1
        elif kind == "result":
            run.session_id = ev.get("session_id") or run.session_id
            u = ev.get("usage") or {}
            run.usage.add(Usage(u.get("input_tokens", 0) or 0, u.get("cache_read_input_tokens", 0) or 0,
                                u.get("cache_creation_input_tokens", 0) or 0, u.get("output_tokens", 0) or 0))
            run.api_equivalent_usd += ev.get("total_cost_usd", 0) or 0
            run.final_text = ev.get("result", "") or ""
            if ev.get("is_error") and run.error is None:
                run.error = f"Claude Code reported an error ({ev.get('subtype')}): {run.final_text[:500]}"
        elif kind == "system" and ev.get("session_id"):
            run.session_id = ev["session_id"]


# --------------------------------------------------------------------------
# Codex
# --------------------------------------------------------------------------

def _toml_str(s: str) -> str:
    return json.dumps(s)   # a JSON string is a valid TOML basic string


# Codex features that would give the model abilities beyond our tools and
# web search: running commands, editing files, your ChatGPT app connectors,
# sub-agents, browser/computer control, plugins... All switched off. (Names
# from `codex features list`; unknown names are ignored by Codex.)
CODEX_DISABLED_FEATURES = [
    "shell_tool", "unified_exec", "unified_exec_tty", "shell_snapshot", "apps", "multi_agent",
    "image_generation", "view_image", "goals", "computer_use", "browser_use", "browser_use_external",
    "in_app_browser", "plugins", "remote_plugin", "skill_search", "tool_suggest",
    "sleep_tool", "hooks", "workspace_dependencies", "memories", "realtime_conversation",
]


class CodexAgent(CliAgent):
    NPM_JS = ["@openai/codex/bin/codex.js"]
    CLI_NAME = "codex"
    LOGIN_CMD = ["login", "status"]
    INSTALL_HINT = "Install: npm install -g @openai/codex"
    LOGIN_HINT = "Sign in with your ChatGPT account: codex login (on a server: codex login --device-auth)"

    def command(self, workdir, mcp, web_search, resume_session):
        env_table = "{" + ", ".join(f"{k} = {_toml_str(v)}" for k, v in mcp.env.items()) + "}"
        overrides = {
            "approval_policy": _toml_str("never"),
            "model_reasoning_effort": _toml_str(SETTINGS.reasoning_effort),
            "web_search": _toml_str("live" if web_search else "disabled"),
            **{f"features.{f}": "false" for f in CODEX_DISABLED_FEATURES},
            "agents.enabled": "false",                          # no sub-agents
            "mcp_servers.arena.command": _toml_str(Path(mcp.command).as_posix()),
            # Nobody is there to click "approve", and with approval_policy
            # "never" Codex would otherwise refuse every call to our tools.
            "mcp_servers.arena.default_tools_approval_mode": _toml_str("approve"),
            "mcp_servers.arena.args": "[" + ", ".join(_toml_str(a) for a in mcp.args) + "]",
            "mcp_servers.arena.env": env_table,
            "mcp_servers.arena.startup_timeout_sec": "60",
            "mcp_servers.arena.tool_timeout_sec": "300",
        }
        argv = resolve_cli("codex", self.NPM_JS) + ["exec", "--json", "--skip-git-repo-check",
                                                    "--ignore-user-config", "--sandbox", SETTINGS.codex_sandbox]
        for k, v in overrides.items():
            argv += ["-c", f"{k}={v}"]
        if self.model:
            argv += ["-m", self.model]
        if resume_session:
            argv += ["resume", resume_session]
        return argv + ["-"]   # "-" = read the prompt from stdin

    def parse_event(self, ev, run):
        kind = ev.get("type")
        if kind == "thread.started":
            run.session_id = ev.get("thread_id")
        elif kind == "item.completed":
            item = ev.get("item") or {}
            t = item.get("type") or item.get("item_type")
            if t == "web_search":
                run.web_searches += 1
            elif t == "command_execution":
                run.shell_commands += 1
            elif t in ("agent_message", "assistant_message"):
                run.final_text = item.get("text", "") or run.final_text
        elif kind == "turn.completed":
            u = ev.get("usage") or {}
            cached = u.get("cached_input_tokens", 0) or 0
            run.usage.add(Usage(input=(u.get("input_tokens", 0) or 0) - cached, cached=cached,
                                output=u.get("output_tokens", 0) or 0))
        elif kind in ("turn.failed", "error") and run.error is None:
            detail = ev.get("error") or ev.get("message")
            run.error = f"Codex error: {json.dumps(detail)[:500]}"


CLI_ADAPTERS: dict[str, type[CliAgent]] = {"claude": ClaudeCodeAgent, "gpt": CodexAgent}

PROBE_PROMPT = ("This is a connectivity test. Call the arena ping tool exactly once, then reply with one "
                "line listing the names of every tool you can use.")


def probe(agent: CliAgent) -> dict:
    """A tiny real turn: can the CLI reach our MCP server, and is it locked
    down (no shell)? Uses a little of your plan's usage."""
    import tempfile

    workdir = Path(tempfile.mkdtemp(prefix="arena_probe_"))
    try:
        result = workdir / "result.json"
        mcp = mcp_launch("-", agent.agent_id, "2000-01-01", 0, 0, str(result), simulate=True, probe=True)
        run = agent.run(PROBE_PROMPT, workdir, mcp, web_search=False, timeout=300)
        pinged = result.exists() and json.loads(result.read_text(encoding="utf-8")).get("pinged", False)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    ok = bool(pinged) and run.shell_commands == 0 and run.error is None
    summary = (f"ping {'reached' if pinged else 'did NOT reach'} the arena tools; "
               f"shell commands run: {run.shell_commands}; "
               + (f"error: {run.error}; " if run.error else "")
               + f"model said: {run.final_text.strip()[:300]!r}")
    return {"ok": ok, "summary": summary, "run": run}
