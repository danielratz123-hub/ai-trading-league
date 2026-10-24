"""Settings, all from environment variables (hub.env on the server), plus
the agent list in hub/agents.yaml.

    HUB_REPO            owner/name, e.g. danielratz123-hub/ai-trading-league
    HUB_GITHUB_TOKEN    fine-grained token for that one repo (see HUB.md)
    HUB_DATA            where the hub keeps its database, logs and work copies
    HUB_BASE_BRANCH     branch PRs go into (main)
    HUB_MAX_ROUNDS      review -> fix rounds before it stops and asks Joe (4)
    HUB_AGENT_TIMEOUT   minutes one agent run may take (45)
    HUB_REVIEW_TIMEOUT  minutes to wait for CI after a review (40)
    HUB_AGENTS_FILE     extra/changed agents, server only (default $HUB_DATA/agents.yaml)
    HUB_MAX_PARALLEL    agent runs at the same time, at most one per agent (2; the page can change it)
    HUB_MIN_FREE_MB     don't start an extra run with less free memory than this (1500)
    HUB_MAX_LOAD        ...or with a CPU load above this per core (0.8)
    HUB_CMD_<AGENT>     optional JSON list replacing one agent's command
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID   optional phone notifications
    CLAUDE_CODE_OAUTH_TOKEN                 Claude's login (claude setup-token)
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger("hub")

AGENTS_FILE = Path(__file__).parent / "agents.yaml"
KEY_RE = re.compile(r"^[a-z][a-z0-9-]{1,20}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,79}$")
LIST_FIELDS = ("cmd", "model_args", "effort_args", "efforts", "models", "list_models", "version",
               "env_keys")


def _clean(key: str, raw: dict, base: dict | None = None) -> dict | None:
    """One agent definition, checked. None if it's unusable."""
    if not KEY_RE.match(key) or not isinstance(raw, dict):
        log.warning("agents: skipping %r (bad name or not a mapping)", key)
        return None
    a = dict(base or {})
    for name in LIST_FIELDS:
        if name in raw:
            val = raw[name]
            if val is None:
                val = []
            if not isinstance(val, list) or not all(isinstance(x, (str, int, float)) for x in val):
                log.warning("agents: %s.%s must be a list of strings", key, name)
                return None
            a[name] = [str(x) for x in val]
    for name in ("label", "color"):
        if name in raw and raw[name] is not None:
            a[name] = str(raw[name])[:40]
    a.setdefault("label", key.title())
    a.setdefault("color", "#A1A1A6")
    for name in LIST_FIELDS:
        a.setdefault(name, [])
    if a["cmd"].count("{prompt}") != 1:
        log.warning("agents: %s.cmd needs exactly one \"{prompt}\"", key)
        return None
    if not a["version"]:
        a["version"] = [a["cmd"][0], "--version"]
    a["models"] = [m for m in a["models"] if MODEL_RE.match(m)]
    return a


def load_agents(extra_file: Path | None = None) -> dict[str, dict]:
    """hub/agents.yaml, then the server's own file on top (new agents, or
    changed fields of existing ones), then HUB_CMD_<AGENT> commands."""
    agents: dict[str, dict] = {}
    for path in (AGENTS_FILE, extra_file):
        if not path or not Path(path).exists():
            continue
        try:
            data = yaml.safe_load(Path(path).read_text()) or {}
        except (OSError, yaml.YAMLError) as e:
            log.warning("agents: can't read %s (%s)", path, e)
            continue
        if not isinstance(data, dict):
            continue
        for key, raw in data.items():
            if raw is False:                       # "codex: false" removes an agent
                agents.pop(str(key), None)
                continue
            a = _clean(str(key), raw, agents.get(str(key)))
            if a:
                agents[str(key)] = a
    for key, a in agents.items():
        raw = os.getenv(f"HUB_CMD_{key.upper().replace('-', '_')}", "").strip()
        if not raw:
            continue
        try:
            cmd = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(cmd, list) and all(isinstance(x, str) for x in cmd) and cmd.count("{prompt}") == 1:
            a["cmd"] = cmd
    return agents


def _int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "").strip() or default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return max(0.1, float(os.getenv(name, "").strip() or default))
    except ValueError:
        return default


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
    deploy_timeout: int = 90 * 60
    poll_seconds: int = 20
    max_parallel: int = 2
    min_free_mb: int = 1500
    max_load: float = 0.8
    telegram_token: str = ""
    telegram_chat: str = ""
    agents: dict = field(default_factory=load_agents)

    @property
    def configured(self) -> bool:
        return bool(self.repo and "/" in self.repo and self.token)

    @property
    def secrets(self) -> list[str]:
        """Values that must never appear in a pushed change: the hub's own
        tokens and every agent's env_keys values."""
        names = {k for a in self.agents.values() for k in a.get("env_keys", [])}
        values = [self.token, self.telegram_token, *(os.getenv(n, "") for n in sorted(names))]
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
    data_dir = Path(os.getenv("HUB_DATA", "/hub/data"))
    extra = Path(os.getenv("HUB_AGENTS_FILE", "").strip() or data_dir / "agents.yaml")
    return Config(
        repo=os.getenv("HUB_REPO", "").strip(),
        token=os.getenv("HUB_GITHUB_TOKEN", "").strip(),
        data_dir=data_dir,
        base_branch=os.getenv("HUB_BASE_BRANCH", "main").strip() or "main",
        max_rounds=_int("HUB_MAX_ROUNDS", 4),
        agent_timeout=_int("HUB_AGENT_TIMEOUT", 45) * 60,
        review_timeout=_int("HUB_REVIEW_TIMEOUT", 40) * 60,
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        max_parallel=min(_int("HUB_MAX_PARALLEL", 2), 3),
        min_free_mb=_int("HUB_MIN_FREE_MB", 1500),
        max_load=_float("HUB_MAX_LOAD", 0.8),
        agents=load_agents(extra),
    )
