"""Settings, all from environment variables (hub.env on the server).

    HUB_REPO            owner/name, e.g. danielratz123-hub/ai-trading-league
    HUB_GITHUB_TOKEN    fine-grained token for that one repo (see HUB.md)
    HUB_DATA            where the hub keeps its database, logs and work copies
    HUB_BASE_BRANCH     branch PRs go into (main)
    HUB_MAX_ROUNDS      review -> fix rounds before it stops and asks Joe (4)
    HUB_AGENT_TIMEOUT   minutes one agent run may take (45)
    HUB_REVIEW_TIMEOUT  minutes to wait for CI + Claude's review (40)
    HUB_CMD_CLAUDE / HUB_CMD_CODEX / HUB_CMD_GEMINI
                        optional JSON list overriding an agent's command;
                        "{prompt}" is replaced by the task prompt
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID   optional phone notifications
    CLAUDE_CODE_OAUTH_TOKEN                 Claude's login (claude setup-token)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# The official CLIs in headless mode. The container is the sandbox: the
# agents may run any command inside their own work copy (tests, ruff), and
# the container has no league data, no .env and no GitHub token in reach.
DEFAULT_AGENTS: dict[str, dict] = {
    "claude": {
        "label": "Claude",
        "cmd": ["claude", "-p", "{prompt}", "--dangerously-skip-permissions"],
        "version": ["claude", "--version"],
        # Its login (from `claude setup-token`) goes to Claude only.
        "env_keys": ["CLAUDE_CODE_OAUTH_TOKEN"],
    },
    "codex": {
        "label": "Codex (GPT)",
        "cmd": ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox",
                "--skip-git-repo-check", "{prompt}"],
        "version": ["codex", "--version"],
    },
    "gemini": {
        "label": "Gemini",
        # Google's Antigravity CLI: signs in with your Gemini subscription
        # (docker compose exec -it hub agy). GEMINI_API_KEY is passed too, for
        # anyone using a key instead (see HUB.md).
        "cmd": ["agy", "-p", "{prompt}", "--dangerously-skip-permissions"],
        "version": ["agy", "--version"],
        "env_keys": ["GEMINI_API_KEY"],
    },
}


def _int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "").strip() or default))
    except ValueError:
        return default


def _agents() -> dict[str, dict]:
    agents = {k: dict(v) for k, v in DEFAULT_AGENTS.items()}
    for key in agents:
        raw = os.getenv(f"HUB_CMD_{key.upper()}", "").strip()
        if not raw:
            continue
        try:
            cmd = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(cmd, list) and cmd and all(isinstance(x, str) for x in cmd):
            agents[key]["cmd"] = cmd
    return agents


@dataclass(frozen=True)
class Config:
    repo: str = ""
    token: str = ""
    data_dir: Path = Path("/hub/data")
    base_branch: str = "main"
    max_rounds: int = 4
    agent_timeout: int = 45 * 60
    check_timeout: int = 20 * 60
    review_timeout: int = 40 * 60
    poll_seconds: int = 20
    telegram_token: str = ""
    telegram_chat: str = ""
    agents: dict = field(default_factory=lambda: {k: dict(v) for k, v in DEFAULT_AGENTS.items()})

    @property
    def configured(self) -> bool:
        return bool(self.repo and "/" in self.repo and self.token)

    @property
    def secrets(self) -> list[str]:
        """Values that must never appear in a pushed change."""
        values = [self.token, self.telegram_token, os.getenv("CLAUDE_CODE_OAUTH_TOKEN", ""),
                  os.getenv("GEMINI_API_KEY", "")]
        return [v for v in values if len(v) >= 12]

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hub.db"


def load() -> Config:
    return Config(
        repo=os.getenv("HUB_REPO", "").strip(),
        token=os.getenv("HUB_GITHUB_TOKEN", "").strip(),
        data_dir=Path(os.getenv("HUB_DATA", "/hub/data")),
        base_branch=os.getenv("HUB_BASE_BRANCH", "main").strip() or "main",
        max_rounds=_int("HUB_MAX_ROUNDS", 4),
        agent_timeout=_int("HUB_AGENT_TIMEOUT", 45) * 60,
        review_timeout=_int("HUB_REVIEW_TIMEOUT", 40) * 60,
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        agents=_agents(),
    )
