"""Phase 5 study: do the adaptive pieces beat the plain versions on history?

    python -m arena.lab adaptive-study

  5A  every sleeve (strategies.SLEEVES) four ways: plain, volatility sizing,
      ATR stops, both
  5B  the five sleeves combined: equal weight vs the regime allocator
  5C  each of those with and without the risk dial

Every comparison is reported out of sample before the locked holdout (as a
whole, per 6-month walk-forward test window, and per market state), and then
on the holdout ONCE: each idea's holdout result is logged in backtest_runs
(purpose 'holdout'); a rerun reuses that logged result instead of looking
again. A piece is recommended only if it beats the plain version on the
league's risk-adjusted score before the holdout AND is not worse on it;
otherwise the plain version stays (and the report says so).

The sleeves are fixed rules with nothing fitted, and the allocator only
uses data before each decision, so one continuous run is split into the two
periods.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

import pandas as pd

from . import adaptive, config as labconfig, engine, runs, schema, walkforward
from .states import labels as state_labels
from .strategies import LIBRARY, SLEEVES

VARIANTS_5A = {"plain": {}, "vol_sizing": {"vol_sizing": True},
               "atr_stops": {"atr_stop_multiple": None}, "both": {"vol_sizing": True, "atr_stop_multiple": None}}


class _Idea:
    """What runs._begin needs to log a study row."""

    def __init__(self, name: str, params: dict):
        self.name, self.params = name, params

    def sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.params, sort_keys=True).encode()).hexdigest()


def _segment_metrics(eq: pd.Series, start: str, end: str, labels: dict, dates: list[str]) -> dict:
    """The league metrics of an equity curve between start and end."""
    before = eq.loc[:pd.Timestamp(start) - pd.Timedelta(days=1)]
    seg = eq.loc[start:end]
    capital = float(before.iloc[-1]) if len(before) else float(seg.iloc[0])
    weeks = [d.strftime("%Y-%m-%d") for i, d in enumerate(seg.index)
             if i == 0 or d.isocalendar()[:2] != seg.index[i - 1].isocalendar()[:2]]
    m = engine.metrics(seg, [], 0, capital, weeks, labels, dates, None)
    return {k: m[k] for k in ("start", "end", "total_return_pct", "annual_return_pct", "max_drawdown_pct", "sharpe",
                              "risk_adjusted_score", "period_score", "by_state")}


def _windows(eq: pd.Series, first: str, locked: str, labels, dates) -> list[dict]:
    out = []
    for w in walkforward.windows(first, locked):
        if pd.Timestamp(w.test_start) < eq.index[0]:
            continue
        m = _segment_metrics(eq, w.test_start, w.test_end, labels, dates)
        out.append({"test": [w.test_start, w.test_end], "score": m["risk_adjusted_score"],
                    "return_pct": m["total_return_pct"]})
    return out


def _holdout_once(conn, key: str, params: dict, compute) -> dict:
    """The holdout result of an idea: computed and logged the first time,
    read back from the log afterwards (the holdout is looked at once)."""
    used = conn.execute("SELECT results FROM backtest_runs WHERE idea_key=? AND purpose='holdout' AND status='ok' "
                        "ORDER BY id LIMIT 1", (key,)).fetchone()
    if used:
        return {**json.loads(used[0]), "reused_logged_result": True}
    rid, n = runs._begin(conn, requested_by="engine:study", idea_key=key, spec=_Idea(key, params),
                         purpose="holdout", start=None, end=None, version_id=None, params=params)
    res = compute()
    runs._finish(conn, rid, "ok", {**res, "run_id": rid, "attempts": n})
    return {**res, "run_id": rid, "attempts": n}


def _log_oos(conn, key: str, params: dict, res: dict) -> int:
    rid, n = runs._begin(conn, requested_by="engine:study", idea_key=key, spec=_Idea(key, params),
                         purpose="walk_forward", start=res.get("start"), end=res.get("end"), version_id=None,
                         params=params)
    runs._finish(conn, rid, "ok", {**res, "run_id": rid, "attempts": n})
    return n


def _verdict(name: str, base: str, oos: dict, hold: dict) -> dict:
    """Recommended only if better on BOTH scores before the holdout and not
    worse on both on it: the league's mean weekly score, and the whole-period
    score (total return / max drawdown). The weekly mean alone can rate a
    strategy that lost most of its money as positive."""
    a, b = oos[name]["risk_adjusted_score"], oos[base]["risk_adjusted_score"]
    pa, pb = oos[name]["period_score"], oos[base]["period_score"]
    ha, hb = hold[name]["risk_adjusted_score"], hold[base]["risk_adjusted_score"]
    hpa, hpb = hold[name]["period_score"], hold[base]["period_score"]
    weekly = a > b and ha >= hb
    period = pa > pb and hpa >= hpb
    better = weekly and period
    return {"use": name if better else base, "better_on_weekly_score": weekly, "better_on_period_score": period,
            "oos_weekly_score": [round(a, 4), round(b, 4)], "oos_period_score": [round(pa, 4), round(pb, 4)],
            "holdout_weekly_score": [round(ha, 4), round(hb, 4)], "holdout_period_score": [round(hpa, 4), round(hpb, 4)],
            "oos_return_pct": [oos[name]["total_return_pct"], oos[base]["total_return_pct"]],
            "oos_max_drawdown_pct": [oos[name]["max_drawdown_pct"], oos[base]["max_drawdown_pct"]],
            "holdout_return_pct": [hold[name]["total_return_pct"], hold[base]["total_return_pct"]],
            "holdout_max_drawdown_pct": [hold[name]["max_drawdown_pct"], hold[base]["max_drawdown_pct"]],
            "why": (f"{name} beat {base} on both scores, before the holdout and on it" if better else
                    f"{name} vs {base}: weekly score {'better' if weekly else 'not better'} "
                    f"({a:.3f} vs {b:.3f}; holdout {ha:.3f} vs {hb:.3f}), whole-period score "
                    f"{'better' if period else 'not better'} ({pa:.3f} vs {pb:.3f}; holdout {hpa:.3f} vs {hpb:.3f}); "
                    f"keep {base}")}


def run(conn: sqlite3.Connection, hconn: sqlite3.Connection, sleeves: tuple[str, ...] = SLEEVES,
        log=print) -> dict:
    schema.migrate(conn)
    d_end = walkforward.data_end(hconn)
    days = [d for d, in hconn.execute("SELECT day FROM hist_calendar WHERE day <= ? ORDER BY day", (d_end,))]
    locked = walkforward.holdout_start(d_end, days=days)
    pre_end = (pd.Timestamp(locked) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    frame = walkforward.load(hconn, d_end)
    dates = [d.strftime("%Y-%m-%d") for d in frame.index]
    labels = state_labels(conn, end=d_end)
    inputs = {r[0]: json.loads(r[1]) for r in conn.execute("SELECT day, inputs FROM market_states WHERE day <= ?",
                                                          (d_end,))}
    first = days[0]
    report: dict = {"data_end": d_end, "holdout_from": locked, "sleeves": list(sleeves),
                    "data_note": labconfig.get().survivorship_note, "settings": labconfig.get().adaptive.__dict__}

    # ---------------------------------------------------------------- 5A
    curves: dict[str, dict[str, pd.Series]] = {}
    report["5A"] = {}
    for name in sleeves:
        fn = LIBRARY[name][0]
        curves[name] = {}
        oos, hold, wins = {}, {}, {}
        for var, kw in VARIANTS_5A.items():
            if "atr_stop_multiple" in kw:
                kw = {**kw, "atr_stop_multiple": labconfig.get().adaptive.atr_multiple}
            log(f"5A {name} / {var}")
            r = engine.run(engine.StrategySpec(name, fn=fn), frame, first, d_end, labels, engine.Options(**kw))
            curves[name][var] = r.equity
            oos[var] = _segment_metrics(r.equity, r.equity.index[0].strftime("%Y-%m-%d"), pre_end, labels, dates)
            wins[var] = _windows(r.equity, first, locked, labels, dates)
            _log_oos(conn, f"adaptive-5a:{name}:{var}", kw, oos[var])
            hold[var] = _holdout_once(conn, f"adaptive-5a:{name}:{var}", kw,
                                      lambda eq=r.equity: _segment_metrics(eq, locked, d_end, labels, dates))
        report["5A"][name] = {"out_of_sample": oos, "holdout": hold, "windows": wins,
                              "vol_sizing": _verdict("vol_sizing", "plain", oos, hold),
                              "atr_stops": _verdict("atr_stops", "plain", oos, hold),
                              "both": _verdict("both", "plain", oos, hold)}

    # ---------------------------------------------------------------- 5B / 5C
    rets = pd.DataFrame({n: curves[n]["plain"] for n in sleeves}).pct_change().dropna(how="all")
    start = rets.dropna().index[0] + pd.Timedelta(days=365)       # a year of history before the first allocation
    start = rets.index[rets.index >= start][0].strftime("%Y-%m-%d")
    combos = {"equal": (False, False), "allocator": (True, False), "equal+dial": (False, True),
              "allocator+dial": (True, True)}
    sims, oos, hold, wins = {}, {}, {}, {}
    for name, (alloc, dial) in combos.items():
        log(f"5B/5C {name}")
        sims[name] = adaptive.simulate(rets, labels, inputs, start, d_end, alloc, dial)
        eq = sims[name].equity
        oos[name] = _segment_metrics(eq, start, pre_end, labels, dates)
        wins[name] = _windows(eq, first, locked, labels, dates)
        _log_oos(conn, f"adaptive-5bc:{name}", {"allocator": alloc, "dial": dial}, oos[name])
        hold[name] = _holdout_once(conn, f"adaptive-5bc:{name}", {"allocator": alloc, "dial": dial},
                                   lambda e=eq: _segment_metrics(e, locked, d_end, labels, dates))
    a = sims["allocator"]
    report["5B_5C"] = {
        "start": start, "out_of_sample": oos, "holdout": hold, "windows": wins,
        "allocator_vs_equal": _verdict("allocator", "equal", oos, hold),
        "dial_vs_none": _verdict("equal+dial", "equal", oos, hold),
        "allocator_dial_vs_equal": _verdict("allocator+dial", "equal", oos, hold),
        "windows_allocator_beat_equal": sum(1 for x, y in zip(wins["allocator"], wins["equal"]) if x["score"] > y["score"]),
        "windows_compared": len(wins["equal"]),
        "allocator_weights_mean": {k: round(float(v), 3) for k, v in a.weights.loc[:pre_end].mean().items()},
        "allocator_weights_range": {k: [round(float(a.weights[k].min()), 3), round(float(a.weights[k].max()), 3)]
                                    for k in a.weights},
        "allocator_rebalances": len(a.rebalances),
        "max_weekly_weight_change": round(float(a.weights.diff().abs().max().max()), 4),
        "dial_days_below_100pct": int((sims["equal+dial"].exposure < 0.999).sum()),
        "dial_min_exposure": round(float(sims["equal+dial"].exposure.min()), 3),
        "turnover": {k: round(v.turnover, 2) for k, v in sims.items()},
    }
    return report
