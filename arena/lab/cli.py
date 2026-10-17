"""python -m arena.lab <command>

    history-download [--symbols A,B] [--force]   first fill of data/history.db (resumable)
    history-update                               append the latest day(s) (the nightly job)
    history-gaps [--through YYYY-MM-DD]          gap check; exit 1 if anything is missing
    market-states [--arena-db PATH]              label every day (SPY only) + coverage check
    backtest --strategy NAME|--code FILE --purpose design|walk_forward|holdout --idea KEY
             [--start D] [--end D] [--grid JSON]  run and log one backtest
    spy-check                                    SPY buy-and-hold vs SPY's own return (engine check)
    repro                                        coded live strategies vs the agents' actual picks
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta

from . import config as labconfig, histdata


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _symbols(args) -> list[str]:
    if args.symbols:
        return sorted({s.strip().upper() for s in args.symbols.split(",") if s.strip()})
    return histdata.universe_symbols()


def _calendar(hconn, source) -> None:
    h = labconfig.get().history
    end = (date.today() + timedelta(days=370)).isoformat()
    n = histdata.store_calendar(hconn, source, h.start, end)
    print(f"calendar: {n} trading days {h.start} .. {end}")


def cmd_history_download(args) -> int:
    source = histdata.AlpacaHistorySource()
    with histdata.connect(args.history_db) as hconn:
        _calendar(hconn, source)
        end = args.through or histdata.last_completed_day(hconn)
        rep = histdata.download(hconn, source, _symbols(args), end, force=args.force)
        _print(rep.to_dict())
        return 1 if rep.failed else 0


def cmd_history_update(args) -> int:
    source = histdata.AlpacaHistorySource()
    with histdata.connect(args.history_db) as hconn:
        _calendar(hconn, source)
        end = args.through or histdata.last_completed_day(hconn)
        rep = histdata.update(hconn, source, _symbols(args), end)
        _print(rep.to_dict())
        if args.arena_db:
            from .states import label_all
            conn = _arena(args)
            try:
                _print({"market_states": label_all(conn, hconn)})
            finally:
                conn.close()
        return 1 if rep.failed else 0


def cmd_history_gaps(args) -> int:
    with histdata.connect(args.history_db) as hconn:
        rep = histdata.gap_check(hconn, args.through)
        _print(rep)
        return 0 if rep.get("ok") else 1


def _arena(args):
    from ..config import SETTINGS
    from ..db import connect
    return connect(args.arena_db or str(SETTINGS.db_path))


def cmd_market_states(args) -> int:
    from . import states
    with histdata.connect(args.history_db) as hconn:
        conn = _arena(args)
        try:
            out = {"labels": states.label_all(conn, hconn)}
            out["coverage"] = states.coverage(conn, hconn, args.through or histdata.last_completed_day(hconn))
        finally:
            conn.close()
    _print(out)
    return 0 if out["coverage"]["ok"] else 1


def _spec(args, params=None):
    from . import engine
    from .strategies import LIBRARY
    if args.code:
        with open(args.code, encoding="utf-8") as f:
            return engine.StrategySpec(args.idea or args.code, code=f.read(), params=params or {})
    if args.strategy not in LIBRARY:
        raise SystemExit(f"unknown strategy {args.strategy!r}; one of {', '.join(LIBRARY)}")
    return engine.StrategySpec(args.strategy, fn=LIBRARY[args.strategy][0], params=params or {})


def cmd_backtest(args) -> int:
    from . import runs
    grid = json.loads(args.grid) if args.grid else None
    with histdata.connect(args.history_db) as hconn:
        conn = _arena(args)
        try:
            res = runs.execute(conn, hconn, make=lambda p: _spec(args, p), grid=grid, purpose=args.purpose,
                               idea_key=args.idea or args.strategy or args.code, requested_by="human",
                               start=args.start, end=args.end)
        finally:
            conn.close()
    _print(res)
    return 0 if res.get("ok") else 1


def cmd_spy_check(args) -> int:
    """Phase 3 check: SPY buy-and-hold through the engine (benchmark mode:
    no stops, no position cap) vs SPY's own open-to-close return."""
    from . import engine, runs
    from .strategies import spy_buy_and_hold
    with histdata.connect(args.history_db) as hconn:
        conn = _arena(args)
        try:
            res = runs.execute(conn, hconn, spec=engine.StrategySpec("spy_buy_and_hold", fn=spy_buy_and_hold),
                               purpose="check", idea_key="engine-check:spy", requested_by="engine",
                               start=args.start, end=args.end, options=engine.Options(benchmark=True))
        finally:
            conn.close()
    if not res.get("ok"):
        _print(res)
        return 1
    diff = res["total_return_pct"] - res["spy_return_pct"]
    out = {k: res[k] for k in ("start", "end", "total_return_pct", "spy_return_pct", "final_equity")}
    out["difference_pct_points"] = round(diff, 3)
    out["tolerance_pct_points"] = 0.5
    out["ok"] = bool(abs(diff) <= 0.5)
    _print(out)
    return 0 if out["ok"] else 1


def cmd_repro(args) -> int:
    from .. import universe
    from . import repro
    with histdata.connect(args.history_db) as hconn:
        conn = _arena(args)
        try:
            decisions = repro.live_decisions(conn)
        finally:
            conn.close()
        end = max(d["decision_date"] for d in decisions.values())
        frame = histdata.bars(hconn, end=end)
    rows = repro.compare(frame, decisions, set(universe.SECTOR_OF))
    _print(rows)
    return 0


COMMANDS = {
    "history-download": cmd_history_download,
    "history-update": cmd_history_update,
    "history-gaps": cmd_history_gaps,
    "market-states": cmd_market_states,
    "backtest": cmd_backtest,
    "spy-check": cmd_spy_check,
    "repro": cmd_repro,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m arena.lab", description="Backtesting lab")
    p.add_argument("command", choices=COMMANDS)
    p.add_argument("--history-db", default=None, help="history database (default data/history.db)")
    p.add_argument("--arena-db", default=None, help="the league database (market-state labels, run log)")
    p.add_argument("--symbols", help="comma-separated subset")
    p.add_argument("--through", help="last day to download / check (default: last completed trading day)")
    p.add_argument("--force", action="store_true", help="history-download: re-download even if done")
    p.add_argument("--strategy", help="backtest: a strategy from arena/lab/strategies.py")
    p.add_argument("--code", help="backtest: a file defining target_weights(prices, date) (runs in the sandbox)")
    p.add_argument("--purpose", default="design", choices=["design", "walk_forward", "holdout"])
    p.add_argument("--idea", help="backtest: the idea's key (attempts are counted per key)")
    p.add_argument("--grid", help='backtest walk_forward: JSON list of params, e.g. [{"stop_loss_pct": 0.05}]')
    p.add_argument("--start", help="backtest / spy-check: first execution day")
    p.add_argument("--end", help="backtest / spy-check: last day")
    return p


def main_args(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return COMMANDS[args.command](args)


def main(argv: list[str] | None = None) -> None:
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main_args(argv))
