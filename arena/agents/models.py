"""Models each agent can use, for the setup page's dropdowns.

ChatGPT: read live from Codex (`codex debug models`), which returns the
catalog for *your* ChatGPT plan. If that fails, a built-in list is used.

Claude: Claude Code has no command that lists models, so this is
Anthropic's current lineup. Which ones your plan allows depends on it;
if a model isn't available, the test turn on the setup page will say so.
"""
from __future__ import annotations

import json
import subprocess
import time

from .cli import CodexAgent, resolve_cli

CLAUDE_MODELS = [
    {"id": "claude-opus-5-5", "name": "Claude Opus 5.5", "tier": "frontier",
     "note": "Default. Strong reasoning at a moderate share of your plan's usage."},
    {"id": "claude-fable-5-1", "name": "Claude Fable 5.1", "tier": "top",
     "note": "Most capable Claude. Uses your plan's limits fastest; some plans may not include it."},
    {"id": "claude-sonnet-5-5", "name": "Claude Sonnet 5.5", "tier": "workhorse",
     "note": "Faster and lighter on usage limits; a bit less thorough."},
    {"id": "claude-haiku-5-5", "name": "Claude Haiku 5.5", "tier": "fast",
     "note": "Fastest and lightest. Weakest at multi-step research."},
    {"id": "claude-fable-5", "name": "Claude Fable 5", "tier": "older", "note": "Previous Fable."},
    {"id": "claude-opus-5", "name": "Claude Opus 5", "tier": "older", "note": "Previous Opus."},
    {"id": "claude-sonnet-5", "name": "Claude Sonnet 5", "tier": "older", "note": "Previous Sonnet."},
    {"id": "claude-opus-4-8", "name": "Claude Opus 4.8", "tier": "older", "note": "Older generation."},
    {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6", "tier": "older", "note": "Older generation."},
    {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5", "tier": "older", "note": "Older generation."},
]

# Used only if `codex debug models` can't be read. Mirrors the catalog seen
# on 2026-10-08.
GPT_FALLBACK = [
    {"id": "gpt-6.1-sol", "name": "GPT-6.1-Sol", "note": "Latest workhorse model for coding and everyday work."},
    {"id": "gpt-6-astra", "name": "GPT-6-Astra", "note": "Frontier intelligence for the most demanding work."},
    {"id": "gpt-6-sol", "name": "GPT-6-Sol", "note": "Previous generation workhorse model."},
    {"id": "gpt-6-luna", "name": "GPT-6-Luna", "note": "Fast and affordable model for easier tasks."},
    {"id": "gpt-5.6-sol", "name": "GPT-5.6-Sol", "note": "Older generation workhorse model."},
    {"id": "gpt-5.6-terra", "name": "GPT-5.6-Terra", "note": "Older balanced model for straightforward work."},
    {"id": "gpt-5.6-luna", "name": "GPT-5.6-Luna", "note": "Older fast and efficient model."},
]

# Roughly equal pairings, for a fair match.
FAIR_PAIRS = [
    {"label": "Strongest vs strongest", "claude": "claude-opus-5-5", "gpt": "gpt-6-astra"},
    {"label": "Everyday workhorses", "claude": "claude-sonnet-5-5", "gpt": "gpt-6.1-sol"},
    {"label": "Fast and light", "claude": "claude-haiku-5-5", "gpt": "gpt-6-luna"},
]

_cache: dict = {}
CACHE_SECONDS = 600


def gpt_models() -> tuple[list[dict], str]:
    """(models, source) where source is 'live' or 'built-in'."""
    if _cache and time.time() - _cache["at"] < CACHE_SECONDS:
        return _cache["models"], _cache["source"]
    models, source = GPT_FALLBACK, "built-in"
    try:
        argv = resolve_cli("codex", CodexAgent.NPM_JS) + ["debug", "models"]
        p = subprocess.run(argv, capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
        data = json.loads(p.stdout[p.stdout.index("{"):] if p.stdout.lstrip().startswith("{") else p.stdout)
        entries = data.get("models", []) if isinstance(data, dict) else data
        live = [{"id": m["slug"], "name": m.get("display_name") or m["slug"], "note": m.get("description", ""),
                 "levels": [lv.get("effort", lv) if isinstance(lv, dict) else lv
                            for lv in m.get("supported_reasoning_levels") or []]}
                for m in entries
                if m.get("visibility") == "list"]   # hidden ones are internal / legacy
        if live:
            models, source = live, "live"
    except (FileNotFoundError, OSError, ValueError, KeyError, subprocess.SubprocessError):
        pass
    _cache.update(models=models, source=source, at=time.time())
    return models, source


def catalog() -> dict:
    gpt, source = gpt_models()
    return {"claude": CLAUDE_MODELS, "gpt": gpt, "gpt_source": source, "pairs": FAIR_PAIRS}
