"""Loads config/lab.yaml into frozen dataclasses.

Like config/learning.yaml: a missing or mistyped key fails loudly instead of
silently using a default. Only a human edits the file (see its header).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from ..config import PROJECT_ROOT, SETTINGS
from ..learning.config import ConfigError, _hm

CONFIG_PATH = PROJECT_ROOT / "config" / "lab.yaml"


@dataclass(frozen=True)
class History:
    db_file: str
    start: str
    feed: str
    batch_symbols: int
    requests_per_minute: int
    max_retries: int
    overlap_days: int
    nightly_update_at: tuple[int, int]
    extra_symbols: tuple[str, ...]


@dataclass(frozen=True)
class MarketState:
    trend_sma_days: int
    vol_days: int
    vol_low_below: float
    vol_high_at: float


@dataclass(frozen=True)
class Costs:
    spread_bps_per_side: float
    slippage_bps_per_side: float
    fractional_min_dollar_volume: float

    @property
    def bps_per_side(self) -> float:
        return self.spread_bps_per_side + self.slippage_bps_per_side


@dataclass(frozen=True)
class Engine:
    starting_cash: float
    rebalance: str
    stop_fill: str
    min_history_days: int


@dataclass(frozen=True)
class WalkForward:
    design_months: int
    test_months: int
    step_months: int


@dataclass(frozen=True)
class LabConfig:
    history: History
    survivorship_note: str
    market_state: MarketState
    costs: Costs
    engine: Engine
    walk_forward: WalkForward
    holdout_months: int


def parse(raw: dict) -> LabConfig:
    try:
        h, ms, c, e, wf = (raw[k] for k in ("history", "market_state", "costs", "engine", "walk_forward"))
        cfg = LabConfig(
            history=History(str(h["db_file"]), str(h["start"]), str(h["feed"]).lower(), int(h["batch_symbols"]),
                            int(h["requests_per_minute"]), int(h["max_retries"]), int(h["overlap_days"]),
                            _hm(h["nightly_update_at"]), tuple(str(s).upper() for s in h["extra_symbols"] or [])),
            survivorship_note=" ".join(str(raw["survivorship_note"]).split()),
            market_state=MarketState(int(ms["trend_sma_days"]), int(ms["vol_days"]), float(ms["vol_low_below"]),
                                     float(ms["vol_high_at"])),
            costs=Costs(float(c["spread_bps_per_side"]), float(c["slippage_bps_per_side"]),
                        float(c["fractional_min_dollar_volume"])),
            engine=Engine(float(e["starting_cash"]), str(e["rebalance"]), str(e["stop_fill"]),
                          int(e["min_history_days"])),
            walk_forward=WalkForward(int(wf["design_months"]), int(wf["test_months"]), int(wf["step_months"])),
            holdout_months=int(raw["holdout"]["months"]),
        )
    except KeyError as err:
        raise ConfigError(f"config/lab.yaml is missing {err}") from None
    if cfg.history.feed not in ("sip", "iex"):
        raise ConfigError("history.feed must be sip or iex")
    if not 0 < cfg.market_state.vol_low_below < cfg.market_state.vol_high_at:
        raise ConfigError("market_state: need 0 < vol_low_below < vol_high_at")
    if cfg.engine.rebalance not in ("weekly", "daily"):
        raise ConfigError("engine.rebalance must be weekly or daily")
    if cfg.engine.stop_fill != "stop_or_open":
        raise ConfigError("engine.stop_fill must be stop_or_open")
    if min(cfg.costs.spread_bps_per_side, cfg.costs.slippage_bps_per_side) < 0:
        raise ConfigError("costs can't be negative")
    if min(cfg.walk_forward.design_months, cfg.walk_forward.test_months, cfg.walk_forward.step_months) < 1:
        raise ConfigError("walk_forward windows must be >= 1 month")
    if cfg.holdout_months < 1:
        raise ConfigError("holdout.months must be >= 1")
    return cfg


def load(path: str | Path = CONFIG_PATH) -> LabConfig:
    with open(path, encoding="utf-8") as f:
        return parse(yaml.safe_load(f))


@lru_cache(maxsize=1)
def get() -> LabConfig:
    return load(os.getenv("ARENA_LAB_CONFIG", str(CONFIG_PATH)))


def history_db_path() -> Path:
    """data/history.db next to the live database, unless HISTORY_DB says otherwise."""
    env = os.getenv("HISTORY_DB", "").strip()
    return Path(env) if env else SETTINGS.db_path.with_name(get().history.db_file)


def history_enabled() -> bool:
    """HISTORY_ENABLED=1 adds the nightly history append to the scheduler."""
    return os.getenv("HISTORY_ENABLED", "0").strip() == "1"
