"""Loads config/learning.yaml into frozen dataclasses.

Every threshold of the learning layer comes from that one file. A missing or
mistyped key fails loudly at startup instead of silently using a default, so
the file is the single source of truth.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from ..config import PROJECT_ROOT

CONFIG_PATH = PROJECT_ROOT / "config" / "learning.yaml"


class ConfigError(ValueError):
    pass


def _hm(s: str) -> tuple[int, int]:
    try:
        h, m = (int(x) for x in str(s).split(":"))
    except ValueError:
        raise ConfigError(f"bad time {s!r}; use HH:MM") from None
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ConfigError(f"bad time {s!r}")
    return h, m


@dataclass(frozen=True)
class Schedule:
    timezone: str
    risk_first: tuple[int, int]
    risk_every_minutes: int
    risk_last: tuple[int, int]
    daily_review: tuple[int, int]
    weekly_review_weekday: int        # Monday=0 .. Sunday=6
    weekly_review: tuple[int, int]
    use_alpaca_calendar: bool


@dataclass(frozen=True)
class Risk:
    stop_loss_pct: float
    gap_alert_pct: float
    vix_spike_pct: float
    strategy_drawdown_limit: float
    refire_after_days: int


@dataclass(frozen=True)
class Regime:
    labels: tuple[str, ...]
    confirm_days: int
    high_vol_vix: float
    high_vol_realized: float
    sma_fast: int
    sma_slow: int
    realized_vol_days: int


@dataclass(frozen=True)
class HardLimits:
    max_position_pct_of_strategy: float
    max_gross_exposure: float
    max_short_exposure_pct: float
    every_position_needs_stop: bool
    max_trades_per_strategy_per_day: int
    allowed_instruments: tuple[str, ...]
    min_price: float
    min_avg_daily_volume: float
    max_daily_loss_pct: float
    max_price_move_since_analysis_pct: float
    trade_cap_includes_weekly_rebalance: bool


@dataclass(frozen=True)
class Learning:
    min_trades_for_rewrite: int
    lesson_confirmations_to_activate: int
    rewrites_per_agent_per_week: int
    cooldown_weeks_after_change: int
    underperform_spy_weeks: int
    shadow_test_weeks: int
    max_tilt_pct: float
    max_lessons_in_prompt: int


@dataclass(frozen=True)
class Scoring:
    mode: str
    drawdown_floor_pct: float
    start_week: int


@dataclass(frozen=True)
class MarketData:
    minute_bar_retention_days: int
    sector_etfs: tuple[str, ...]
    vix_daily_url: str
    vix_intraday_proxy: str
    vix_proxy_scale: float
    headlines: int
    watchlist_max: int


@dataclass(frozen=True)
class LearningConfig:
    schedule: Schedule
    risk: Risk
    regime: Regime
    param_ranges: dict[str, tuple[float, float]]
    hard_limits: HardLimits
    learning: Learning
    scoring: Scoring
    market_data: MarketData

    def risk_check_times(self, close_hm: tuple[int, int] = (16, 0)) -> list[tuple[int, int]]:
        """The day's risk-check times: first, first+every, ... and `last`,
        none later than 30 minutes before the close (half days)."""
        s = self.schedule
        limit = min(s.risk_last[0] * 60 + s.risk_last[1], close_hm[0] * 60 + close_hm[1] - 30)
        t = s.risk_first[0] * 60 + s.risk_first[1]
        out = []
        while t <= limit:
            out.append(t)
            t += s.risk_every_minutes
        if limit not in out and limit >= s.risk_first[0] * 60 + s.risk_first[1]:
            out.append(limit)
        return [(m // 60, m % 60) for m in sorted(out)]


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _range(name: str, v) -> tuple[float, float]:
    if not (isinstance(v, (list, tuple)) and len(v) == 2 and all(isinstance(x, (int, float)) for x in v)
            and v[0] <= v[1]):
        raise ConfigError(f"param_ranges.{name} must be [low, high]")
    return float(v[0]), float(v[1])


def parse(raw: dict) -> LearningConfig:
    try:
        s, r, g, h, l, sc, md = (raw[k] for k in ("schedule", "risk", "regime", "hard_limits", "learning",
                                                 "scoring", "market_data"))
        day, hm = str(s["weekly_review"]).split()
        if day.lower() not in _WEEKDAYS:
            raise ConfigError(f"schedule.weekly_review: unknown weekday {day!r}")
        cfg = LearningConfig(
            schedule=Schedule(str(s["timezone"]), _hm(s["risk_check"]["first"]),
                              int(s["risk_check"]["every_minutes"]), _hm(s["risk_check"]["last"]),
                              _hm(s["daily_review"]), _WEEKDAYS.index(day.lower()), _hm(hm),
                              bool(s["use_alpaca_calendar"])),
            risk=Risk(float(r["stop_loss_pct"]), float(r["gap_alert_pct"]), float(r["vix_spike_pct"]),
                      float(r["strategy_drawdown_limit"]), int(r["refire_after_days"])),
            regime=Regime(tuple(g["labels"]), int(g["confirm_days"]), float(g["high_vol_vix"]),
                          float(g["high_vol_realized"]), int(g["sma_fast"]), int(g["sma_slow"]),
                          int(g["realized_vol_days"])),
            param_ranges={k: _range(k, v) for k, v in raw["param_ranges"].items()},
            hard_limits=HardLimits(float(h["max_position_pct_of_strategy"]), float(h["max_gross_exposure"]),
                                   float(h["max_short_exposure_pct"]), bool(h["every_position_needs_stop"]),
                                   int(h["max_trades_per_strategy_per_day"]), tuple(h["allowed_instruments"]),
                                   float(h["min_price"]), float(h["min_avg_daily_volume"]),
                                   float(h["max_daily_loss_pct"]), float(h["max_price_move_since_analysis_pct"]),
                                   bool(h["trade_cap_includes_weekly_rebalance"])),
            learning=Learning(int(l["min_trades_for_rewrite"]), int(l["lesson_confirmations_to_activate"]),
                              int(l["rewrites_per_agent_per_week"]), int(l["cooldown_weeks_after_change"]),
                              int(l["underperform_spy_weeks"]), int(l["shadow_test_weeks"]),
                              float(l["max_tilt_pct"]), int(l["max_lessons_in_prompt"])),
            scoring=Scoring(str(sc["mode"]), float(sc["drawdown_floor_pct"]), int(sc["start_week"])),
            market_data=MarketData(int(md["minute_bar_retention_days"]), tuple(md["sector_etfs"]),
                                   str(md["vix_daily_url"]), str(md["vix_intraday_proxy"]),
                                   float(md["vix_proxy_scale"]), int(md["headlines"]), int(md["watchlist_max"])),
        )
    except KeyError as e:
        raise ConfigError(f"config/learning.yaml is missing {e}") from None
    if set(cfg.regime.labels) != {"trend_up", "trend_down", "choppy", "high_vol"}:
        raise ConfigError("regime.labels must be exactly trend_up, trend_down, choppy, high_vol")
    if cfg.scoring.mode not in ("risk_adjusted", "raw_return"):
        raise ConfigError("scoring.mode must be risk_adjusted or raw_return")
    bad = set(cfg.hard_limits.allowed_instruments) - {"us_stocks", "us_etfs"}
    if bad:
        raise ConfigError(f"hard_limits.allowed_instruments: unknown {sorted(bad)}")
    for name in ("stop_loss_pct", "position_max_pct"):
        if name not in cfg.param_ranges:
            raise ConfigError(f"param_ranges.{name} is required")
    lo, hi = cfg.param_ranges["position_max_pct"]
    if hi > cfg.hard_limits.max_position_pct_of_strategy + 1e-12:
        raise ConfigError("param_ranges.position_max_pct can't exceed hard_limits.max_position_pct_of_strategy")
    return cfg


def load(path: str | Path = CONFIG_PATH) -> LearningConfig:
    with open(path, encoding="utf-8") as f:
        return parse(yaml.safe_load(f))


@lru_cache(maxsize=1)
def get() -> LearningConfig:
    return load(os.getenv("ARENA_LEARNING_CONFIG", str(CONFIG_PATH)))


def enabled() -> bool:
    """LEARNING_ENABLED=1 in .env switches the layer on (read at call time)."""
    return os.getenv("LEARNING_ENABLED", "0").strip() == "1"


def default_params() -> dict[str, float]:
    """A new strategy's starting params: the risk defaults, clipped into range."""
    c = get()
    lo, hi = c.param_ranges["stop_loss_pct"]
    plo, phi = c.param_ranges["position_max_pct"]
    return {"stop_loss_pct": min(max(c.risk.stop_loss_pct, lo), hi),
            "position_max_pct": min(max(c.hard_limits.max_position_pct_of_strategy, plo), phi)}
