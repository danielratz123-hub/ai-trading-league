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
class Evidence:
    backtest_budget: int
    freeze_min_live_trades: int
    freeze_min_weeks: int
    min_oos_trades: int
    regime_margin_pct: float
    min_state_days: int
    holdout_margin: float
    shadow_min_trades: int
    shadow_score_margin: float
    shadow_bootstrap_prob: float
    shadow_bootstrap_samples: int
    shadow_max_weeks: int
    lesson_min_backtest_trades: int
    lesson_min_live_trades: int
    hypothesis_max_weeks: int


@dataclass(frozen=True)
class Adaptive:
    vol_days: int
    atr_days: int
    atr_multiple: float
    atr_multiple_range: tuple[float, float]
    min_stop_pct: float
    max_stop_pct: float
    lookback_days: int
    min_regime_days: int
    shrink_days: int
    max_tilt: float
    min_weight: float
    max_weight: float
    max_change: float
    confirm_days: int
    market_exposure: float
    drawdown_levels: tuple[tuple[float, float], ...]
    recover_per_week: float


@dataclass(frozen=True)
class LabConfig:
    history: History
    survivorship_note: str
    market_state: MarketState
    costs: Costs
    engine: Engine
    walk_forward: WalkForward
    holdout_months: int
    evidence: Evidence
    adaptive: Adaptive


def _adaptive(a: dict) -> "Adaptive":
    vs, at, al, rd = a["vol_sizing"], a["atr_stops"], a["allocator"], a["risk_dial"]
    lo, hi = (float(x) for x in at["multiple_range"])
    out = Adaptive(int(vs["vol_days"]), int(at["atr_days"]), float(at["multiple"]), (lo, hi),
                   float(at["min_stop_pct"]), float(at["max_stop_pct"]), int(al["lookback_days"]),
                   int(al["min_regime_days"]), int(al["shrink_days"]), float(al["max_tilt"]), float(al["min_weight"]),
                   float(al["max_weight"]), float(al["max_change"]), int(al["confirm_days"]),
                   float(rd["market_exposure"]), tuple((float(d), float(e)) for d, e in rd["drawdown_levels"]),
                   float(rd["recover_per_week"]))
    if not lo <= out.atr_multiple <= hi:
        raise ConfigError("adaptive.atr_stops.multiple must be inside multiple_range")
    if not 0 < out.min_stop_pct < out.max_stop_pct < 1:
        raise ConfigError("adaptive.atr_stops: need 0 < min_stop_pct < max_stop_pct < 1")
    if not 0 <= out.min_weight < out.max_weight <= 1:
        raise ConfigError("adaptive.allocator: need 0 <= min_weight < max_weight <= 1")
    if not all(0 < e <= 1 for _, e in out.drawdown_levels) or not 0 < out.market_exposure <= 1:
        raise ConfigError("adaptive.risk_dial: exposures must be in (0, 1] (the dial only cuts risk)")
    return out


def _evidence(ev: dict) -> Evidence:
    fz, pr, sh, ls = ev["freeze"], ev["promotion"], ev["shadow"], ev["lessons"]
    out = Evidence(int(ev["backtest_budget_per_agent_per_week"]), int(fz["min_live_trades"]), int(fz["min_weeks"]),
                   int(pr["min_oos_trades"]), float(pr["regime_margin_pct"]), int(pr["min_state_days"]),
                   float(pr["holdout_margin"]), int(sh["min_trades"]), float(sh["score_margin"]),
                   float(sh["bootstrap_prob"]), int(sh["bootstrap_samples"]), int(sh["max_weeks"]),
                   int(ls["min_backtest_trades"]), int(ls["min_live_trades"]), int(ls["hypothesis_max_weeks"]))
    if not 0.5 <= out.shadow_bootstrap_prob < 1:
        raise ConfigError("evidence.shadow.bootstrap_prob must be in [0.5, 1)")
    if min(out.backtest_budget, out.shadow_bootstrap_samples, out.shadow_max_weeks) < 1:
        raise ConfigError("evidence: budget, bootstrap_samples and max_weeks must be >= 1")
    return out


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
            evidence=_evidence(raw["evidence"]),
            adaptive=_adaptive(raw["adaptive"]),
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


def history_db_for(arena_db: str | Path) -> Path:
    """history.db next to a given league database (the MCP server gets --db)."""
    env = os.getenv("HISTORY_DB", "").strip()
    return Path(env) if env else Path(arena_db).with_name(get().history.db_file)


def evidence_enabled() -> bool:
    """EVIDENCE_RULES=1 (with LEARNING_ENABLED=1) switches Phase 4 on. Read at call time."""
    from ..learning import config as lconfig
    return os.getenv("EVIDENCE_RULES", "0").strip() == "1" and lconfig.enabled()


def history_enabled() -> bool:
    """HISTORY_ENABLED=1 adds the nightly history append to the scheduler."""
    return os.getenv("HISTORY_ENABLED", "0").strip() == "1"
