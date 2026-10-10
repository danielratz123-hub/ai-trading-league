"""Hourly risk check during market hours: plain code, no LLM unless a rule fires.

Rules (config/learning.yaml `risk:`), on the freshest 1-minute prices:
  stop_loss          a position is down more than its strategy's stop_loss_pct
                     from its average entry price (default risk.stop_loss_pct)
  gap_alert          a position moved more than gap_alert_pct since the last
                     check (the first check of the day compares with the
                     previous close)
  vix_spike          the VIX is up more than vix_spike_pct today (measured on
                     the VIXY proxy x vix_proxy_scale; fires for every agent
                     holding positions)
  strategy_drawdown  a strategy's equity is more than strategy_drawdown_limit
                     below its peak since it started -> also flagged for an
                     emergency rewrite in the next daily review

Nothing fired: the check is logged (risk_checks) and nothing else happens.
Something fired: only the affected agent is woken (a 'risk' review). It may
reduce or close positions or raise cash, filled at once at the latest
1-minute price; it can't buy or change strategy rules. The same rule for the
same position wakes the agent at most once per refire_after_days.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from typing import Callable

from .. import ledger
from ..db import log_event, transaction
from ..prices import ET
from . import collector, config as lconfig, execution, grader, journal, review, strategies as strat


def _peak(conn, agent_id: str, s: strat.Strategy, d: str) -> float | None:
    start = datetime.fromisoformat(s.created_at).astimezone(ET).date().isoformat()
    rows = conn.execute("SELECT slots_json FROM snapshots WHERE agent_id=? AND kind IN ('daily','post_trade') "
                        "AND date>=? AND date<=?", (agent_id, start, d)).fetchall()
    vals = [json.loads(r[0]).get(str(s.slot)) for r in rows]
    vals = [v for v in vals if v]
    return max(vals) if vals else None


def evaluate(conn: sqlite3.Connection, d: str, as_of: str, mctx: dict) -> tuple[list[dict], int, dict]:
    """All rules for all agents. Returns (events, positions checked, valuations)."""
    cfg = lconfig.get()
    events: list[dict] = []
    vals = {}
    checked = 0
    last_seen = {r["ticker"]: (r["ts"], r["price"]) for r in conn.execute("SELECT * FROM risk_last_prices")}
    vix_change = mctx.get("vix_proxy_change")
    for agent_id in ledger.agent_ids(conn):
        val = collector.mark_slots(conn, agent_id, as_of, d)
        vals[agent_id] = val
        by_slot = {s.slot: s for s in strat.active(conn, agent_id)}
        positions = ledger.positions(conn, agent_id)
        for p in positions:
            t, slot = p["ticker"], p["slot"]
            fresh = collector.latest_minute_price(conn, t, as_of)
            if fresh is None:
                continue
            checked += 1
            px = fresh[0]
            s = by_slot.get(slot)
            sl = (s.params.get("stop_loss_pct") if s else None) or cfg.risk.stop_loss_pct
            long = p["shares"] > 0
            loss = (p["avg_price"] - px) / p["avg_price"] if long else (px - p["avg_price"]) / p["avg_price"]
            if p["avg_price"] > 0 and loss >= sl:
                events.append({"agent_id": agent_id, "slot": slot, "ticker": t, "rule": "stop_loss",
                               "value": round(loss, 4), "threshold": sl,
                               "detail": f"{t} at {px:.2f} is {loss:.1%} against the entry {p['avg_price']:.2f}"})
            ref = last_seen.get(t)
            base = ref[1] if ref and datetime.fromisoformat(ref[0]).astimezone(ET).date().isoformat() == d \
                else collector.prev_close(conn, t, d)
            if base:
                move = px / base - 1
                if abs(move) >= cfg.risk.gap_alert_pct:
                    events.append({"agent_id": agent_id, "slot": slot, "ticker": t, "rule": "gap_alert",
                                   "value": round(move, 4), "threshold": cfg.risk.gap_alert_pct,
                                   "detail": f"{t} moved {move:+.1%} since {'the last check' if ref else 'the previous close'}"})
        for slot, s in by_slot.items():
            eq = val.slot_equity.get(slot, 0.0)
            peak = max(_peak(conn, agent_id, s, d) or 0.0, eq)
            if peak > 0 and eq > 0 and (1 - eq / peak) >= cfg.risk.strategy_drawdown_limit:
                events.append({"agent_id": agent_id, "slot": slot, "ticker": None, "rule": "strategy_drawdown",
                               "value": round(1 - eq / peak, 4), "threshold": cfg.risk.strategy_drawdown_limit,
                               "detail": f"strategy '{s.name}' is {1 - eq / peak:.1%} below its peak ${peak:,.2f}"})
        threshold = cfg.risk.vix_spike_pct * 100 * cfg.market_data.vix_proxy_scale
        if positions and vix_change is not None and vix_change >= threshold:
            events.append({"agent_id": agent_id, "slot": None, "ticker": None, "rule": "vix_spike",
                           "value": vix_change, "threshold": threshold,
                           "detail": f"{cfg.market_data.vix_intraday_proxy} up {vix_change:.1f}% today "
                                     f"(~VIX +{vix_change / cfg.market_data.vix_proxy_scale:.0f}%)"})
    return events, checked, vals


def _suppressed(conn, e: dict, d: str) -> bool:
    days = lconfig.get().risk.refire_after_days
    rows = conn.execute("SELECT day FROM risk_events WHERE agent_id=? AND rule=? AND COALESCE(slot,-1)=? "
                        "AND COALESCE(ticker,'')=? AND suppressed=0", (e["agent_id"], e["rule"], e["slot"] if e["slot"] is not None else -1,
                                                                       e["ticker"] or "")).fetchall()
    return any((date.fromisoformat(d) - date.fromisoformat(r[0])).days < days for r in rows)


def run_check(conn: sqlite3.Connection, d: str, now: datetime | None = None,
              runner: review.Runner | None = None, dry_run: bool = False,
              after_fills: Callable[[sqlite3.Connection, str], None] | None = None) -> dict:
    """One hourly check. runner=None: only log (never wake anyone)."""
    now = now or datetime.now(timezone.utc)
    as_of = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    mctx = collector.intraday_context(conn, d, now)
    events, checked, vals = evaluate(conn, d, as_of, mctx)
    with transaction(conn):
        collector.write_context(conn, mctx)
        for agent_id, val in vals.items():
            grader.write_portfolio_snapshots(conn, agent_id, as_of, val)
        for e in events:
            e["suppressed"] = _suppressed(conn, e, d)
        woke = sorted({e["agent_id"] for e in events if not e["suppressed"]})
        check_id = conn.execute("INSERT INTO risk_checks(ts, day, positions, fired, woke, note) VALUES (?,?,?,?,?,?)",
                                (as_of, d, checked, len(events), json.dumps(woke if runner else []),
                                 None if runner else "no agent runner: logged only")).lastrowid
        for e in events:
            conn.execute("INSERT INTO risk_events(check_id, ts, day, agent_id, slot, ticker, rule, value, threshold, "
                         "detail, suppressed) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (check_id, as_of, d, e["agent_id"], e["slot"], e["ticker"], e["rule"], e["value"],
                          e["threshold"], e["detail"], int(e["suppressed"])))
            if e["rule"] == "strategy_drawdown":
                s = strat.in_slot(conn, e["agent_id"], e["slot"])
                if s and strat.flag_emergency(conn, s.strategy_id, e["detail"], as_of):
                    log_event(conn, "warning", f"risk:{e['agent_id']}",
                              f"slot {e['slot']} flagged for an emergency rewrite: {e['detail']}")
                    from .. import alerts
                    alerts.send(f"Drawdown limit hit ({e['agent_id']}, slot {e['slot']}): {e['detail']}. "
                                f"Flagged for an emergency rewrite in the next daily review.", level="error")
        for agent_id, val in vals.items():
            for t, px in val.prices.items():
                fresh = collector.latest_minute_price(conn, t, as_of)
                if fresh:
                    conn.execute("INSERT OR REPLACE INTO risk_last_prices(ticker, ts, price) VALUES (?,?,?)",
                                 (t, as_of, fresh[0]))
    out = {"day": d, "ts": as_of, "positions_checked": checked, "fired": len(events),
           "events": [{k: e[k] for k in ("agent_id", "slot", "ticker", "rule", "detail", "suppressed")} for e in events],
           "woken": [], "fills": 0}
    if runner is None:
        return out
    from ..league import is_paused
    for agent_id in woke:
        if is_paused(conn, agent_id):
            out["woken"].append({"agent": agent_id, "status": "not woken: paused (kill switch)"})
            continue
        mine = [e for e in events if e["agent_id"] == agent_id and not e["suppressed"]]
        situation = {"fired": [{k: e[k] for k in ("slot", "ticker", "rule", "value", "threshold", "detail")} for e in mine],
                     "intraday_market": {"spy_change_pct": mctx["spy_change"], "vix_proxy_change_pct": mctx["vix_proxy_change"]},
                     "prices_now": {t: round(p, 4) for t, p in vals[agent_id].prices.items()}}
        fills_before = conn.execute("SELECT COUNT(*) FROM trades WHERE reason='risk_exit'").fetchone()[0]
        res = review.run_review(conn, agent_id, "risk", d, runner, as_of=as_of, dry_run=dry_run,
                                situation=situation, on_risk_actions=_actions_handler(vals[agent_id], mine))
        out["woken"].append(res)
        out["fills"] += conn.execute("SELECT COUNT(*) FROM trades WHERE reason='risk_exit'").fetchone()[0] - fills_before
    if out["fills"] and after_fills and not dry_run:
        after_fills(conn, d)
    return out


def _actions_handler(val: ledger.Valuation, fired: list[dict]):
    """Executes a risk review's actions (inside run_review's transaction)."""
    def handle(ctx: review.Ctx, raw: dict | None, fallback_error: str | None) -> list[int]:
        conn, agent_id = ctx.conn, ctx.agent_id
        ids: list[int] = []
        affected = sorted({e["slot"] for e in fired if e["slot"] is not None}) or \
            sorted({p["slot"] for p in ledger.positions(conn, agent_id)})

        def row(slot, action, reasoning, conf, symbol=None, weight=None, fallback=False):
            s = strat.in_slot(conn, agent_id, slot)
            if s is None:
                return None
            i = journal.record(conn, agent_id=agent_id, version_id=s.version_id, checkpoint="risk_exit",
                               input_sha=ctx.input_sha, input_kind="risk_review", reasoning=reasoning, action=action,
                               confidence=conf, symbol=symbol, target_weight=weight, regime=ctx.regime,
                               lessons_used=ctx.lesson_ids, fallback=fallback, source_ref=f"review:{ctx.run_id}",
                               decided_at=ctx.as_of)
            ids.append(i)
            return i

        if raw is None:
            for slot in affected:
                row(slot, "hold", f"FALLBACK to hold: {fallback_error}", None, fallback=True)
            return ids
        conf = float(raw["confidence"])
        acted: set[int] = set()
        for a in raw["actions"]:
            slot = a["slot"]
            if a["type"] == "hold":
                continue
            if a["type"] == "raise_cash":
                legs = [(p["ticker"], a["fraction"]) for p in ledger.positions(conn, agent_id)
                        if p["slot"] == slot and p["shares"] > 0]
            else:
                legs = [(a["ticker"].upper().strip(), 1.0 if a["type"] == "close" else a["fraction"])]
            for ticker, frac in legs:
                held = ledger.position_shares(conn, agent_id, slot, ticker)
                if abs(held) < ledger.DUST:
                    ctx.reject(slot, "risk_action", a, f"no position in {ticker}")
                    continue
                fresh = collector.latest_minute_price(conn, ticker, ctx.as_of)
                if fresh is None:
                    ctx.reject(slot, "risk_action", a, f"no fresh price for {ticker}; nothing traded")
                    continue
                s_eq = val.slot_equity.get(slot, 0.0)
                left = held * (1 - frac) * fresh[0] / s_eq if s_eq > 0 else None
                dec = row(slot, "sell" if held > 0 else "cover", raw["reasoning"], conf, ticker,
                          None if left is None else round(left, 6))
                tr = execution.reduce_now(conn, agent_id, slot, ticker, frac, ctx.day, fresh[0],
                                          f"risk exit ({a['type']}): {raw['reasoning'][:200]}", dec)
                if tr:
                    acted.add(slot)
                    ctx.applied.append(f"slot {slot} {ticker}: {tr.shares:+.4f} sh @ {tr.fill_price:.2f}")
        for slot in affected:
            if slot not in acted:
                row(slot, "hold", raw["reasoning"], conf)
        return ids
    return handle
