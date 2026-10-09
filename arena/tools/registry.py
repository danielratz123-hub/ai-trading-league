"""The research toolbox handed to each agent for its weekly turn.

Tool definitions are written once here in a provider-neutral form (name,
description, JSON schema). The phase-3 adapters convert them to Anthropic's
and OpenAI's tool formats, so both agents get exactly the same tools.

web_search is special: each provider has its own built-in web search, so it
isn't dispatched here. The adapter attaches the provider's search tool when
`web_search_enabled` is True (never in simulate mode, where it would leak
the future).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from ..config import SETTINGS
from . import data_tools, sandbox_tools
from .context import ResearchContext
from .data_tools import ToolError
from .indicators import KINDS

MAX_RESULT_CHARS = 20_000

_IND_LIST = ", ".join(f"{k}_N ({d})" if w else f"{k} ({d})" for k, (w, d) in KINDS.items())

TOOL_SPECS: list[dict] = [
    {
        "name": "get_prices",
        "description": (
            "Daily OHLCV bars (split- and dividend-adjusted) up to the decision date. "
            f"Max {data_tools.MAX_TICKERS} tickers; results are capped at "
            f"{data_tools.MAX_PRICE_ROWS} rows (you'll be told if truncated). "
            "Default range: last 90 days. Returns CSV."),
        "parameters": {
            "type": "object",
            "properties": {
                "tickers": {"type": "array", "items": {"type": "string"}},
                "start": {"type": "string", "description": "YYYY-MM-DD"},
                "end": {"type": "string", "description": "YYYY-MM-DD (capped at decision date)"},
                "fields": {"type": "array", "items": {"type": "string",
                           "enum": ["open", "high", "low", "close", "volume"]},
                           "description": "default ['close']"},
            },
            "required": ["tickers"],
        },
    },
    {
        "name": "get_indicators",
        "description": (
            "Technical indicators for up to 50 tickers, latest values by default. Windows are in "
            f"trading days (21~1m, 63~3m, 252~1y). Available: {_IND_LIST}. "
            "Set params.history_days to get a time series instead (max 260)."),
        "parameters": {
            "type": "object",
            "properties": {
                "tickers": {"type": "array", "items": {"type": "string"}},
                "indicators": {"type": "array", "items": {"type": "string"},
                               "description": "e.g. ['return_63', 'sma_200', 'rsi_14']"},
                "params": {"type": "object", "properties": {"history_days": {"type": "integer"}}},
            },
            "required": ["tickers", "indicators"],
        },
    },
    {
        "name": "screen",
        "description": (
            "Filter and rank the whole universe. Each condition is '<lhs> <op> <rhs>' with ops "
            "> < >= <= == !=, where each side is an indicator name, a number, or "
            "pct_rank(<indicator>) (0-1 percentile across the screened tickers). Also "
            "'sector == Energy'. Example: ['price > sma_200', 'pct_rank(return_63) >= 0.9']. "
            "sort_by: '<indicator> [asc|desc]' (default desc)."),
        "parameters": {
            "type": "object",
            "properties": {
                "conditions": {"type": "array", "items": {"type": "string"}},
                "sort_by": {"type": "string"},
                "limit": {"type": "integer", "description": "1-100, default 20"},
                "universe": {"type": "string", "enum": ["all", "sp500", "etf"]},
            },
            "required": ["conditions"],
        },
    },
    {
        "name": "market_overview",
        "description": ("Snapshot of the market at the decision date: index returns, SPY vs "
                        "50/200-day MA, volatility regime, sector ranking, breadth "
                        "(% of S&P 500 above 50/200-day MA), bonds and gold."),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "run_python",
        "description": (
            "Run Python analysis code in a sandbox and get its printed output. Only pandas, numpy, "
            "math and statistics can be imported; no files, no network. Predefined variables: "
            "`prices` (DataFrame, DatetimeIndex, MultiIndex columns (field, ticker) with fields "
            "open/high/low/close/volume; e.g. prices['close']['AAPL']), `universe` "
            "(dict ticker -> sector), `decision_date` (str). Data ends at the decision date. "
            "Limits: 30s CPU, output truncated at 8000 chars. Use print()."),
        "parameters": {"type": "object", "properties": {"code": {"type": "string"}},
                       "required": ["code"]},
    },
    {
        "name": "backtest",
        "description": (
            "Backtest an idea on history before the decision date with the league's real costs "
            "(commission, 5 bps slippage, short borrow fee), rebalanced weekly at Monday's open "
            "using data up to the prior close. Pass EITHER `code` (defining "
            "target_weights(prices, date) -> dict, same as a code slot) OR `portfolio` "
            "(fixed weights, e.g. {'SPY': 0.6, 'TLT': 0.4}). Returns total/annualized return, "
            "Sharpe, max drawdown, turnover, fees, and SPY's return over the same period."),
        "parameters": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "YYYY-MM-DD"},
                "end": {"type": "string", "description": "YYYY-MM-DD (default: decision date)"},
                "code": {"type": "string"},
                "portfolio": {"type": "object", "additionalProperties": {"type": "number"}},
            },
            "required": ["start"],
        },
    },
    {
        "name": "save_strategy_code",
        "description": (
            "Save code for a code slot (1-5). The code must define "
            "target_weights(prices: pd.DataFrame, date: str) -> dict[str, float] returning "
            "weights within the slot (negative = short, sum of |weights| <= 1). `prices` has the "
            "same layout as in run_python and only contains data up to `date`. The engine runs "
            "it every week at decision time. It's test-run now; the result is returned."),
        "parameters": {
            "type": "object",
            "properties": {"slot": {"type": "integer"}, "code": {"type": "string"}},
            "required": ["slot", "code"],
        },
    },
]

WEB_SEARCH_SPEC = {
    "name": "web_search",
    "description": "Search the web for news and research (provider built-in tool).",
    "builtin": True,
}


@dataclass
class Budget:
    max_calls: int = SETTINGS.max_tool_calls
    max_spend_usd: float = SETTINGS.max_api_spend_usd
    calls: int = 0
    spend_usd: float = 0.0

    @property
    def exhausted(self) -> bool:
        return self.calls >= self.max_calls or self.spend_usd >= self.max_spend_usd

    def status(self) -> str:
        return (f"{self.calls}/{self.max_calls} tool calls, "
                f"${self.spend_usd:.3f}/${self.max_spend_usd:.2f} API spend")


BUDGET_EXHAUSTED = ("Research budget exhausted ({status}). No more research tools can be used; "
                    "call submit_decision now with your final decision.")


class ToolBox:
    def __init__(self, ctx: ResearchContext, budget: Budget | None = None):
        self.ctx = ctx
        self.budget = budget or Budget()
        self._handlers: dict[str, Callable[..., Any]] = {
            "get_prices": lambda a: data_tools.get_prices(
                ctx, a["tickers"], a.get("start"), a.get("end"), a.get("fields")),
            "get_indicators": lambda a: data_tools.get_indicators(
                ctx, a["tickers"], a["indicators"], a.get("params")),
            "screen": lambda a: data_tools.screen(
                ctx, a["conditions"], a.get("sort_by"), a.get("limit", 20), a.get("universe", "all")),
            "market_overview": lambda a: data_tools.market_overview(ctx),
            "run_python": lambda a: sandbox_tools.run_python(ctx, a["code"]),
            "backtest": lambda a: sandbox_tools.backtest(
                ctx, a["start"], a.get("end"), a.get("code"), a.get("portfolio")),
            "save_strategy_code": lambda a: sandbox_tools.save_strategy_code(
                ctx, int(a["slot"]), a["code"]),
        }

    @property
    def web_search_enabled(self) -> bool:
        return not self.ctx.simulate

    def specs(self) -> list[dict]:
        return list(TOOL_SPECS)

    def call(self, name: str, args: dict | None) -> str:
        """Run one tool and return its result as a string for the model.

        Errors never raise: they come back as text so the agent can correct
        itself. Every call (successful or not) counts against the budget.
        """
        if self.budget.exhausted:
            return BUDGET_EXHAUSTED.format(status=self.budget.status())
        self.budget.calls += 1
        args = args or {}
        started = time.perf_counter()
        handler = self._handlers.get(name)
        ok = True
        try:
            if handler is None:
                raise ToolError(f"Unknown tool {name!r}.")
            result: Any = handler(args)
        except ToolError as e:
            ok, result = False, {"error": str(e)}
        except KeyError as e:
            ok, result = False, {"error": f"Missing required argument {e}."}
        except Exception as e:  # noqa: BLE001 - a tool bug shouldn't kill the agent's turn
            ok, result = False, {"error": f"Tool failed: {type(e).__name__}: {e}"}
        if isinstance(result, dict) and result.get("ok") is False:
            ok = False
        text = json.dumps(result, default=str)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + f'... [result truncated at {MAX_RESULT_CHARS} chars]'
        self._log(name, args, ok, text, int((time.perf_counter() - started) * 1000))
        remaining = self.budget.max_calls - self.budget.calls
        return text + f"\n[budget: {remaining} tool calls left]"

    def _log(self, name: str, args: dict, ok: bool, text: str, ms: int) -> None:
        self.ctx.conn.execute(
            "INSERT INTO tool_calls(agent_id, decision_date, tool, args_json, ok, result_summary, "
            "duration_ms) VALUES (?,?,?,?,?,?,?)",
            (self.ctx.agent_id, self.ctx.decision_date, name, json.dumps(args)[:4000], int(ok),
             text[:300], ms))
