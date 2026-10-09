"""All tunable settings in one place.

Rules of the competition live here so they can be changed without touching
engine code. API keys are *not* stored on the Settings object: they are read
from the environment only at the moment a client is created, so they can't
accidentally end up in a log line, a briefing, or a repr().
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Limits:
    max_gross: float = 1.00          # |longs| + |shorts| <= 100% of equity (no leverage)
    max_short_total: float = 0.50    # all shorts together
    max_long_per_ticker: float = 0.25
    max_short_per_ticker: float = 0.15
    max_slots: int = 5
    # Forced-cover triggers (checked after each close). They are looser than
    # the hard limits so normal daily drift doesn't cause constant covering.
    forced_cover_single_short: float = 0.20
    forced_cover_gross: float = 1.20


@dataclass(frozen=True)
class Costs:
    commission_per_share: float = 0.0035
    commission_min: float = 0.35
    commission_max_pct: float = 0.01   # cap: 1% of trade value
    slippage_bps: float = 5.0
    min_trade_value: float = 1.0       # trades below this are skipped
    borrow_fee_annual: float = 0.005   # 0.5%/year on short market value


@dataclass(frozen=True)
class Settings:
    db_path: Path = PROJECT_ROOT / "data" / "arena.db"
    starting_cash: float = 1000.0
    agents: tuple[str, ...] = ("claude", "gpt")
    models: dict = field(default_factory=lambda: {
        "claude": os.getenv("CLAUDE_MODEL", "claude-opus-5-5"),
        # Empty = the CLI's default model for your ChatGPT account.
        "gpt": os.getenv("GPT_MODEL", ""),
    })
    # "cli" = run agents through Claude Code / Codex on your subscriptions.
    # "api" = pay-per-token API keys (ANTHROPIC_API_KEY / OPENAI_API_KEY).
    agent_backend: str = os.getenv("AGENT_BACKEND", "cli")
    cli_turn_timeout_sec: int = 1800    # one agent's whole weekly turn
    # Codex's own sandbox. Inside Docker its Linux sandbox may not be
    # available; then set CODEX_SANDBOX=danger-full-access (its shell tool is
    # switched off anyway, and the container is the sandbox).
    codex_sandbox: str = os.getenv("CODEX_SANDBOX", "read-only")
    # The Alpaca paper mirror is configured with MIRROR_MODE (see mirror.py).
    cli_max_turns: int = 80             # model round-trips per turn (Claude Code)
    # Fairness: both agents run with the same reasoning effort. (Current
    # Claude Opus and OpenAI reasoning models reject `temperature`, so effort
    # is the shared knob instead.)
    reasoning_effort: str = os.getenv("REASONING_EFFORT", "medium")   # low | medium | high
    max_output_tokens: int = 32000      # per model call (includes thinking)
    # If Claude's safety classifier declines a request, let the API re-run it
    # on Anthropic's recommended fallback model instead of failing the week.
    claude_refusal_fallback: bool = True
    limits: Limits = field(default_factory=Limits)
    costs: Costs = field(default_factory=Costs)

    # Scoring
    bonus_every_weeks: int = 4
    bonus_amount: float = 1000.0

    # Research budget per agent per week (phase 2/3)
    max_tool_calls: int = 40            # research tools + web searches
    max_api_spend_usd: float = float(os.getenv("MAX_API_SPEND_USD", "1.0"))
    # After the budget runs out the agent still needs a call or two to submit.
    # These cap how far past the budget a turn may go before it's abandoned.
    max_calls_after_budget: int = 3
    hard_spend_multiplier: float = 1.5
    max_decision_retries: int = 2       # resubmits after a validation failure
    max_nudges: int = 2                 # "please call submit_decision" reminders

    # USD per million tokens: (input, cached input read, cache write, output).
    # Override GPT prices in .env when you pick a model (GPT_PRICE_IN etc.).
    prices_per_mtok: dict = field(default_factory=lambda: {
        "claude-opus-5-5": (4.00, 0.20, 5.00, 20.00),
        "claude-opus-5": (5.00, 0.50, 6.25, 25.00),
        "claude-sonnet-5-5": (2.00, 0.20, 2.50, 10.00),
        "gpt": (float(os.getenv("GPT_PRICE_IN", "1.25")),
                float(os.getenv("GPT_PRICE_CACHED_IN", "0.125")),
                float(os.getenv("GPT_PRICE_IN", "1.25")),   # OpenAI has no cache-write surcharge
                float(os.getenv("GPT_PRICE_OUT", "10.00"))),
    })
    web_search_price_usd: float = 0.01   # per search, both providers

    # Price data
    price_history_start: str = "2015-01-01"
    data_feed: str = os.getenv("ALPACA_DATA_FEED", "sip")  # falls back to iex automatically


SETTINGS = Settings()


class MissingKeyError(RuntimeError):
    pass


def require_env(name: str) -> str:
    """Return a secret from the environment, or fail with a helpful message.

    The message names the variable but never echoes any value.
    """
    value = os.getenv(name, "").strip()
    if not value:
        raise MissingKeyError(
            f"{name} is not set. Add it to {PROJECT_ROOT / '.env'} "
            f"(see .env.example)."
        )
    return value
