"""Agent adapters.

Two backends (SETTINGS.agent_backend):
  "cli" (default): Claude Code and Codex CLIs, on your subscriptions.
  "api": pay-per-token API adapters.

To add a player: write an adapter class for each backend you want, register
it below, and add its id to SETTINGS.agents and its model to SETTINGS.models.
"""
from __future__ import annotations

from ..config import SETTINGS
from .base import AgentAdapter
from .claude import ClaudeAgent
from .cli import CLI_ADAPTERS, CliAgent
from .gpt import GPTAgent

ADAPTERS: dict[str, type[AgentAdapter]] = {
    "claude": ClaudeAgent,
    "gpt": GPTAgent,
}


def make_agent(agent_id: str) -> AgentAdapter | CliAgent:
    table = CLI_ADAPTERS if SETTINGS.agent_backend == "cli" else ADAPTERS
    if agent_id not in table:
        raise ValueError(f"No {SETTINGS.agent_backend} adapter for agent {agent_id!r}")
    return table[agent_id](agent_id, SETTINGS.models[agent_id])
