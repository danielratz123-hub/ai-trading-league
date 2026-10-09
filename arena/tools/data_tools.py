"""Market-data research tools. Each returns a JSON-able dict.

All data comes from ResearchContext.frame(), which is already cut off at the
decision date, so these functions cannot see the future even by accident.
"""
from __future__ import annotations

import math
import re

import numpy as np
import pandas as pd

from ..universe import ETFS, SECTOR_ETF, SECTOR_OF, SP500
from . import indicators as ind
from .context import FIELDS, ResearchContext

MAX_PRICE_ROWS = 1500
MAX_TICKERS = 50
MAX_HISTORY_ROWS = 260


class ToolError(ValueError):
    """A mistake in the agent's arguments; the message is shown to the agent."""


def _r(x, nd=4):
    """Round for compact output; NaN -> None so it's valid JSON."""
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return None
    if isinstance(x, (float, np.floating)):
        return round(float(x), nd)
    return x


def _check_tickers(ctx: ResearchContext, tickers) -> list[str]:
    if isinstance(tickers, str):
        tickers = [t for t in re.split(r"[,\s]+", tickers) if t]
    if not tickers:
        raise ToolError("Give at least one ticker.")
    tickers = [t.upper().strip() for t in tickers]
    if len(tickers) > MAX_TICKERS:
        raise ToolError(f"At most {MAX_TICKERS} tickers per call (got {len(tickers)}).")
    unknown = [t for t in tickers if t not in SECTOR_OF]
    if unknown:
        raise ToolError(f"Not in the universe: {unknown}.")
    have = set(ctx.field("close").columns)
    missing = [t for t in tickers if t not in have]
    if missing:
        raise ToolError(f"No price data for {missing}.")
    return tickers


# --------------------------------------------------------------------------

def get_prices(ctx: ResearchContext, tickers, start: str | None = None, end: str | None = None,
               fields: list[str] | None = None) -> dict:
    tickers = _check_tickers(ctx, tickers)
    fields = [f.lower() for f in (fields or ["close"])]
    bad = [f for f in fields if f not in FIELDS]
    if bad:
        raise ToolError(f"Unknown fields {bad}; use {FIELDS}.")
    notes = []
    if end and end > ctx.decision_date:
        notes.append(f"end {end} is after the decision date; data stops at {ctx.decision_date}.")
    end = min(end or ctx.decision_date, ctx.decision_date)
    start = start or (pd.Timestamp(end) - pd.Timedelta(days=90)).strftime("%Y-%m-%d")

    df = ctx.frame().loc[start:end, (fields, tickers)]
    long = df.stack(level="ticker", future_stack=True).dropna(how="all").reset_index()
    long["date"] = long["date"].dt.strftime("%Y-%m-%d")
    long = long[["date", "ticker", *fields]]
    total = len(long)
    if total > MAX_PRICE_ROWS:
        long = long.iloc[-MAX_PRICE_ROWS:]
        notes.append(f"TRUNCATED: {total} rows matched, showing the most recent {MAX_PRICE_ROWS}. "
                     f"Narrow the date range or ticker list, or use run_python for bulk analysis.")
    for f in fields:
        long[f] = long[f].round(4 if f != "volume" else 0)
    return {"rows": len(long), "notes": notes,
            "csv": long.to_csv(index=False, lineterminator="\n")}


def get_indicators(ctx: ResearchContext, tickers, indicators: list[str],
                   params: dict | None = None) -> dict:
    """Latest values, or with params={"history_days": N} the last N values."""
    tickers = _check_tickers(ctx, tickers)
    if not indicators:
        raise ToolError("Give at least one indicator.")
    try:
        for n in indicators:
            ind.parse(n)
    except ValueError as e:
        raise ToolError(str(e)) from None
    params = params or {}
    hist = int(params.get("history_days", 0) or 0)
    close, volume = ctx.field("close"), ctx.field("volume")
    as_of = close.index[-1].strftime("%Y-%m-%d")

    if hist <= 0:
        table = ind.latest(indicators, close, volume).loc[tickers]
        return {"as_of": as_of, "values": {t: {k: _r(v) for k, v in row.items()}
                                            for t, row in table.iterrows()}}
    hist = min(hist, MAX_HISTORY_ROWS)
    tail = ind.lookback_needed(indicators) + hist
    out = {}
    for n in indicators:
        s = ind.compute(n, close.iloc[-tail:], volume.iloc[-tail:])[tickers].iloc[-hist:]
        s.index = s.index.strftime("%Y-%m-%d")
        out[n] = s.round(4).to_csv(lineterminator="\n")
    return {"as_of": as_of, "history_days": hist, "series_csv": out}


# --------------------------------------------------------------------------
# screen
# --------------------------------------------------------------------------

_COND = re.compile(r"^\s*(.+?)\s*(>=|<=|==|!=|>|<)\s*(.+?)\s*$")
_RANK = re.compile(r"^pct_rank\(\s*([a-z0-9_]+)\s*\)$")
_OPS = {">": np.greater, "<": np.less, ">=": np.greater_equal, "<=": np.less_equal,
        "==": np.equal, "!=": np.not_equal}


def _operand(expr: str, table: pd.DataFrame, needed: set[str]):
    """Return either a column name we need to compute or a number."""
    e = expr.strip().lower()
    try:
        return float(e)
    except ValueError:
        pass
    m = _RANK.match(e)
    if m:
        needed.add(m.group(1))
        return ("rank", m.group(1))
    ind.parse(e)
    needed.add(e)
    return ("col", e)


def screen(ctx: ResearchContext, conditions: list[str], sort_by: str | None = None,
           limit: int = 20, universe: str = "all") -> dict:
    """Filter the universe. Conditions look like:
        "price > sma_200"   "return_63 > 0.1"   "pct_rank(return_63) >= 0.9"
        "rsi_14 < 30"       "sector == Energy"  "sector != Utilities"
    """
    limit = max(1, min(int(limit or 20), 100))
    close, volume = ctx.field("close"), ctx.field("volume")
    pool = {"all": list(SECTOR_OF), "sp500": SP500, "etf": list(ETFS)}.get(universe)
    if pool is None:
        raise ToolError("universe must be 'all', 'sp500' or 'etf'.")
    # Only tickers that traded on the last day (skips delisted names).
    alive = close.iloc[-1].dropna().index
    pool = [t for t in pool if t in alive]

    parsed, needed = [], set()
    for c in conditions or []:
        m = _COND.match(c)
        if not m:
            raise ToolError(f"Can't parse condition {c!r}. Use '<lhs> <op> <rhs>'.")
        lhs, op, rhs = m.groups()
        if lhs.strip().lower() == "sector":
            if op not in ("==", "!="):
                raise ToolError("sector only supports == and !=")
            parsed.append(("sector", op, rhs.strip()))
            continue
        try:
            parsed.append((_operand(lhs, None, needed), op, _operand(rhs, None, needed)))
        except ValueError as e:
            raise ToolError(f"In {c!r}: {e}") from None

    sort_col, ascending = None, False
    if sort_by:
        parts = sort_by.strip().lower().split()
        sort_col = parts[0].lstrip("-")
        ascending = (len(parts) > 1 and parts[1] == "asc")
        try:
            ind.parse(sort_col)
        except ValueError as e:
            raise ToolError(str(e)) from None
        needed.add(sort_col)

    table = ind.latest(sorted(needed), close, volume).loc[pool] if needed else pd.DataFrame(index=pool)
    table["sector"] = [SECTOR_OF[t] for t in table.index]
    mask = pd.Series(True, index=table.index)
    for lhs, op, rhs in parsed:
        if lhs == "sector":
            hit = table["sector"].str.lower() == rhs.lower()
            mask &= hit if op == "==" else ~hit
            continue
        a, b = (_values(x, table) for x in (lhs, rhs))
        mask &= pd.Series(_OPS[op](a, b), index=table.index).fillna(False).astype(bool)

    result = table[mask]
    if sort_col:
        result = result.sort_values(sort_col, ascending=ascending, na_position="last")
    shown = result.head(limit)
    return {
        "as_of": close.index[-1].strftime("%Y-%m-%d"),
        "matched": int(len(result)), "shown": int(len(shown)),
        "results": [{"ticker": t, **{k: _r(v) for k, v in row.items()}} for t, row in shown.iterrows()],
    }


def _values(operand, table: pd.DataFrame):
    if isinstance(operand, float):
        return operand
    kind, col = operand
    s = table[col]
    return s.rank(pct=True) if kind == "rank" else s


# --------------------------------------------------------------------------
# market_overview
# --------------------------------------------------------------------------

def market_overview(ctx: ResearchContext) -> dict:
    close = ctx.field("close")
    last = close.index[-1]

    def ret(t, days):
        s = close[t].dropna() if t in close else pd.Series(dtype=float)
        return _r(s.iloc[-1] / s.iloc[-1 - days] - 1) if len(s) > days else None

    def ytd(t):
        s = close[t].dropna() if t in close else pd.Series(dtype=float)
        prior = s[s.index.year < last.year]
        return _r(s.iloc[-1] / prior.iloc[-1] - 1) if len(prior) and len(s) else None

    out: dict = {"as_of": last.strftime("%Y-%m-%d"), "indexes": {}, "macro": {}}
    for t in ["SPY", "QQQ", "IWM", "DIA"]:
        if t in close:
            out["indexes"][t] = {"1w": ret(t, 5), "1m": ret(t, 21), "3m": ret(t, 63), "ytd": ytd(t)}
    for t in ["TLT", "IEF", "GLD"]:
        if t in close:
            out["macro"][t] = {"1w": ret(t, 5), "1m": ret(t, 21), "3m": ret(t, 63)}

    if "SPY" in close:
        spy = close["SPY"].dropna()
        sma50, sma200 = spy.rolling(50).mean().iloc[-1], spy.rolling(200).mean().iloc[-1]
        daily = spy.pct_change(fill_method=None)
        vol20 = daily.rolling(20).std() * np.sqrt(252)
        hist = vol20.iloc[-252:].dropna()
        pct = float((hist < vol20.iloc[-1]).mean()) if len(hist) else None
        regime = None if pct is None else ("high" if pct > 0.8 else "low" if pct < 0.2 else "normal")
        out["spy_trend"] = {
            "price": _r(spy.iloc[-1], 2), "sma50": _r(sma50, 2), "sma200": _r(sma200, 2),
            "above_sma50": bool(spy.iloc[-1] > sma50), "above_sma200": bool(spy.iloc[-1] > sma200),
            "drawdown_from_52w_high": _r(spy.iloc[-1] / spy.iloc[-252:].max() - 1),
        }
        out["volatility"] = {"spy_realized_vol_20d": _r(vol20.iloc[-1]),
                             "percentile_vs_last_year": _r(pct, 2), "regime": regime}

    sectors = []
    for name, etf in SECTOR_ETF.items():
        if etf in close:
            sectors.append({"sector": name, "etf": etf, "1m": ret(etf, 21), "3m": ret(etf, 63)})
    sectors.sort(key=lambda s: -(s["3m"] if s["3m"] is not None else -9))
    out["sectors_by_3m_return"] = sectors

    members = [t for t in SP500 if t in close.columns]
    c = close[members].iloc[-210:]
    now = c.iloc[-1]
    valid = now.notna()
    out["breadth"] = {
        "pct_above_sma50": _r(float((now > c.rolling(50).mean().iloc[-1])[valid].mean()), 3),
        "pct_above_sma200": _r(float((now > c.rolling(200).mean().iloc[-1])[valid].mean()), 3),
        "pct_positive_1m": _r(float((now / c.iloc[-22] - 1 > 0)[valid].mean()), 3),
        "members_with_data": int(valid.sum()),
    }
    return out
