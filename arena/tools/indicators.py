"""Technical indicators, computed on wide frames (index = date, columns = tickers).

Names follow "<kind>_<window>", e.g. sma_200, return_63, rsi_14. The window
is in trading days (21 ~ 1 month, 63 ~ 3 months, 252 ~ 1 year).
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

BENCH = "SPY"

# kind -> (default window, description shown to agents)
KINDS: dict[str, tuple[int | None, str]] = {
    "price":        (None, "last close"),
    "return":       (21,   "total return over N days"),
    "sma":          (50,   "simple moving average of close"),
    "ema":          (20,   "exponential moving average of close"),
    "dist_sma":     (200,  "close / SMA_N - 1 (e.g. 0.05 = 5% above)"),
    "rsi":          (14,   "Wilder RSI, 0-100"),
    "vol":          (20,   "annualized volatility of daily returns"),
    "drawdown":     (252,  "close / highest close of last N days - 1"),
    "rs":           (63,   "return_N minus SPY's return_N (relative strength)"),
    "volume_ratio": (20,   "5-day avg volume / N-day avg volume"),
    "beta":         (252,  "beta of daily returns vs SPY over N days"),
    "dollar_volume": (20,  "avg daily $ volume over N days"),
}

_NAME = re.compile(r"^([a-z_]+?)(?:_(\d+))?$")


def parse(name: str) -> tuple[str, int | None]:
    m = _NAME.match(name.strip().lower())
    if not m or m.group(1) not in KINDS:
        raise ValueError(f"Unknown indicator {name!r}. Available: "
                         + ", ".join(f"{k}_N" if d else k for k, (d, _) in KINDS.items()))
    kind, win = m.group(1), m.group(2)
    default = KINDS[kind][0]
    if default is None:
        return kind, None
    w = int(win) if win else default
    if not 1 <= w <= 2520:
        raise ValueError(f"{name}: window must be between 1 and 2520 days")
    return kind, w


def lookback_needed(names: list[str]) -> int:
    """How many rows of history the indicators need (with some slack)."""
    most = 1
    for n in names:
        kind, w = parse(n)
        most = max(most, (w or 1) * (3 if kind in ("ema", "rsi") else 1) + 1)
    return most + 5


def compute(name: str, close: pd.DataFrame, volume: pd.DataFrame | None = None) -> pd.DataFrame:
    """Full time series of one indicator for every column."""
    kind, w = parse(name)
    if kind == "price":
        return close
    if kind == "return":
        return close / close.shift(w) - 1
    if kind == "sma":
        return close.rolling(w).mean()
    if kind == "ema":
        return close.ewm(span=w, adjust=False, min_periods=w).mean()
    if kind == "dist_sma":
        return close / close.rolling(w).mean() - 1
    if kind == "rsi":
        diff = close.diff()
        gain = diff.clip(lower=0).ewm(alpha=1 / w, adjust=False, min_periods=w).mean()
        loss = (-diff.clip(upper=0)).ewm(alpha=1 / w, adjust=False, min_periods=w).mean()
        rs = gain / loss.replace(0, np.nan)
        return (100 - 100 / (1 + rs)).where(loss != 0, 100.0)
    if kind == "vol":
        return close.pct_change(fill_method=None).rolling(w).std() * np.sqrt(252)
    if kind == "drawdown":
        return close / close.rolling(w, min_periods=1).max() - 1
    if kind == "rs":
        ret = close / close.shift(w) - 1
        if BENCH not in close.columns:
            raise ValueError("rs needs SPY prices")
        return ret.sub(ret[BENCH], axis=0)
    if kind == "volume_ratio":
        if volume is None:
            raise ValueError("volume_ratio needs volume data")
        return volume.rolling(5).mean() / volume.rolling(w).mean()
    if kind == "dollar_volume":
        if volume is None:
            raise ValueError("dollar_volume needs volume data")
        return (close * volume).rolling(w).mean()
    if kind == "beta":
        r = close.pct_change(fill_method=None)
        if BENCH not in r.columns:
            raise ValueError("beta needs SPY prices")
        cov = r.rolling(w).cov(r[BENCH])
        return cov.div(r[BENCH].rolling(w).var(), axis=0)
    raise AssertionError(kind)


def latest(names: list[str], close: pd.DataFrame, volume: pd.DataFrame | None) -> pd.DataFrame:
    """Latest value of each indicator: index = ticker, columns = indicator."""
    tail = lookback_needed(names)
    c = close.iloc[-tail:]
    v = volume.iloc[-tail:] if volume is not None else None
    out = {}
    for n in names:
        series = compute(n, c, v)
        # A ticker's last bar may be older than the last date (halt, new
        # listing); use its most recent non-missing value.
        out[n] = series.ffill(limit=5).iloc[-1]
    return pd.DataFrame(out)
