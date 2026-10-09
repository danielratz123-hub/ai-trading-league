"""Read-only queries behind the dashboard API. Each returns plain
JSON-ready data, so they're easy to test without a web server.

Fairness rule: an agent's decisions, reasoning and code for a week are only
revealed once that week has been scored (see weeks()).
"""
from __future__ import annotations

import json
import math
import sqlite3

from .. import ledger, scoring
from ..config import SETTINGS
from ..prices import get_price, latest_price_date


def _r(x, nd=6):
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return None
    return round(float(x), nd)


def as_of(conn) -> str | None:
    """The date every dashboard number refers to: the latest snapshot (normally
    today's close). Using one date everywhere keeps comparisons honest, e.g.
    an agent's return vs SPY over exactly the same period."""
    d = conn.execute("SELECT MAX(date) FROM snapshots").fetchone()[0]
    return d or latest_price_date(conn)


def league_status(conn) -> dict:
    paused = conn.execute("SELECT value FROM league_state WHERE key='paused'").fetchone()
    week = conn.execute("SELECT * FROM weeks ORDER BY week_no DESC LIMIT 1").fetchone()
    return {
        "paused": bool(paused and paused[0] == "1"),
        "prices_up_to": latest_price_date(conn),
        "as_of": as_of(conn),
        "agents": [{"id": a, "model": conn.execute("SELECT model FROM agents WHERE agent_id=?", (a,)).fetchone()[0]}
                   for a in ledger.agent_ids(conn)],
        "latest_week": dict(week) if week else None,
        "starting_cash": SETTINGS.starting_cash,
        "bonus_every_weeks": SETTINGS.bonus_every_weeks,
        "bonus_amount": SETTINGS.bonus_amount,
    }


# --------------------------------------------------------------------------
# Performance
# --------------------------------------------------------------------------

def _performance(conn, agent_id: str) -> list[dict]:
    """Daily points with equity and a deposit-free performance index.

    The index chains returns between consecutive snapshots with deposits
    removed, so a $1,000 bonus shows up in equity but not in performance.
    """
    snaps = conn.execute("SELECT * FROM snapshots WHERE agent_id=? ORDER BY seq", (agent_id,)).fetchall()
    deps = conn.execute("SELECT seq, amount FROM deposits WHERE agent_id=? ORDER BY seq", (agent_id,)).fetchall()
    points, index, prev = [], 1.0, None
    for s in snaps:
        if prev is not None and prev["equity"] > 0:
            added = sum(d["amount"] for d in deps if prev["seq"] < d["seq"] < s["seq"])
            index *= (s["equity"] - added) / prev["equity"]
        prev = s
        if s["kind"] == "daily" or not points:
            points.append({"date": s["date"], "equity": _r(s["equity"], 2), "index": _r(index)})
    # One point per day: keep the last snapshot of each date.
    by_date = {}
    for p in points:
        by_date[p["date"]] = p
    return list(by_date.values())


def _spy_index(conn, dates: list[str], start_date: str | None) -> list[float | None]:
    """SPY growth over the same dates, starting from its open on start_date
    (the day the agents first traded)."""
    if not dates or not start_date:
        return []
    base = get_price(conn, "SPY", start_date, "open")
    if not base:
        return [None] * len(dates)
    out = []
    for d in dates:
        c = get_price(conn, "SPY", d, "close")
        out.append(_r(c / base) if c else None)
    return out


def _first_trade_date(conn) -> str | None:
    return conn.execute("SELECT MIN(date) FROM snapshots WHERE kind='post_trade'").fetchone()[0]


def scoreboard(conn) -> dict:
    d = as_of(conn)
    start = _first_trade_date(conn)
    spy = None
    if d and start:
        base, last = get_price(conn, "SPY", start, "open"), get_price(conn, "SPY", d, "close")
        spy = _r(last / base - 1) if base and last else None
    rows = []
    for row in scoring.scoreboard(conn, d) if d else []:
        perf = _performance(conn, row["agent"])
        since_start = perf[-1]["index"] - 1 if perf else None
        rows.append({**{k: _r(v) if isinstance(v, float) else v for k, v in row.items()},
                     "return_since_start": _r(since_start),
                     "vs_spy": _r(since_start - spy) if since_start is not None and spy is not None else None})
    return {"as_of": d, "since": start, "spy_return": spy, "agents": rows}


def equity_curves(conn) -> dict:
    series = {a: {p["date"]: p for p in _performance(conn, a)} for a in ledger.agent_ids(conn)}
    dates = sorted({d for by_date in series.values() for d in by_date})
    spy = _spy_index(conn, dates, _first_trade_date(conn))
    return {
        "dates": dates,
        "agents": {a: {"equity": [by_date[d]["equity"] if d in by_date else None for d in dates],
                       "index": [by_date[d]["index"] if d in by_date else None for d in dates]}
                   for a, by_date in series.items()},
        "spy_index": spy,
        "starting_cash": SETTINGS.starting_cash,
    }


def slot_curves(conn, agent_id: str) -> dict:
    snaps = conn.execute("SELECT date, slots_json FROM snapshots WHERE agent_id=? AND kind='daily' ORDER BY seq",
                         (agent_id,)).fetchall()
    names = {r["slot"]: r["name"] for r in conn.execute("SELECT slot, name FROM slots WHERE agent_id=?", (agent_id,))}
    dates = [s["date"] for s in snaps]
    slots: dict[str, list] = {}
    for i, s in enumerate(snaps):
        for slot, eq in json.loads(s["slots_json"]).items():
            slots.setdefault(slot, [None] * len(snaps))[i] = _r(eq, 2)
    return {"dates": dates, "slots": [{"slot": int(k), "name": "cash reserve" if k == "0" else names.get(int(k), ""),
                                      "equity": v} for k, v in sorted(slots.items(), key=lambda kv: int(kv[0]))]}


# --------------------------------------------------------------------------
# One agent
# --------------------------------------------------------------------------

def agent_detail(conn, agent_id: str) -> dict:
    d = as_of(conn)
    val = ledger.value_agent(conn, agent_id, d, "close")
    eq = val.equity
    L = SETTINGS.limits
    risk = {(r["slot"], r["ticker"]): r for r in conn.execute(
        "SELECT * FROM risk_orders WHERE agent_id=?", (agent_id,))}
    holdings = []
    for p in ledger.positions(conn, agent_id):
        px = val.prices[p["ticker"]]
        v = p["shares"] * px
        r = risk.get((p["slot"], p["ticker"]))
        holdings.append({
            "slot": p["slot"], "ticker": p["ticker"], "side": "long" if p["shares"] > 0 else "short",
            "shares": _r(p["shares"]), "avg_price": _r(p["avg_price"], 4), "last": _r(px, 4),
            "value": _r(v, 2), "pct_equity": _r(v / eq) if eq > 0 else None,
            "unrealized_pnl": _r((px - p["avg_price"]) * p["shares"], 2),
            "stop_loss": r["stop_loss"] if r else None, "take_profit": r["take_profit"] if r else None,
        })
    slots = []
    for s in conn.execute("SELECT * FROM slots WHERE agent_id=? AND (active=1 OR slot=0) ORDER BY slot", (agent_id,)):
        code = None
        if s["type"] == "code":
            row = conn.execute("SELECT code FROM strategy_code WHERE agent_id=? AND slot=? ORDER BY id DESC LIMIT 1",
                               (agent_id, s["slot"])).fetchone()
            code = row["code"] if row else None
        s_eq = val.slot_equity.get(s["slot"], 0.0)
        slots.append({"slot": s["slot"], "name": s["name"] if s["slot"] else "cash reserve", "type": s["type"],
                      "target_allocation": _r(s["allocation"]), "equity": _r(s_eq, 2),
                      "pct_equity": _r(s_eq / eq) if eq > 0 else None,
                      "since_inception": _r(scoring.slot_since_inception(conn, agent_id, s["slot"])) if s["slot"] else None,
                      "code": code})
    tv = val.ticker_value
    longest = max(tv.items(), key=lambda kv: kv[1], default=(None, 0.0))
    shortest = min(tv.items(), key=lambda kv: kv[1], default=(None, 0.0))

    # `limit` is checked when a decision is submitted; prices can then drift
    # past it. `trigger` (where set) is the level at which the engine
    # force-covers at the next open.
    def gauge(label, value, limit, trigger=None, ticker=None):
        return {"label": label, "value": _r(value), "limit": limit, "trigger": trigger, "ticker": ticker}
    exposures = [] if eq <= 0 else [
        gauge("Gross exposure", val.gross / eq, L.max_gross, L.forced_cover_gross),
        gauge("Total short", val.short_value / eq, L.max_short_total),
        gauge("Largest long", max(longest[1], 0) / eq, L.max_long_per_ticker,
              ticker=longest[0] if longest[1] > 0 else None),
        gauge("Largest short", max(-shortest[1], 0) / eq, L.max_short_per_ticker, L.forced_cover_single_short,
              ticker=shortest[0] if shortest[1] < 0 else None),
    ]
    return {"agent": agent_id, "as_of": d, "equity": _r(eq, 2), "cash": _r(val.cash, 2),
            "long_value": _r(val.long_value, 2), "short_value": _r(val.short_value, 2),
            "slots": slots, "holdings": holdings, "exposures": exposures,
            "recent_trades": trades(conn, agent_id, limit=20), "costs": cost_totals(conn, agent_id)}


def trades(conn, agent_id: str | None = None, reason: str | None = None, limit: int = 100) -> list[dict]:
    sql, params = "SELECT * FROM trades WHERE 1=1", []
    if agent_id:
        sql += " AND agent_id=?"
        params.append(agent_id)
    if reason:
        sql += " AND reason=?"
        params.append(reason)
    sql += " ORDER BY seq DESC LIMIT ?"
    params.append(max(1, min(int(limit), 1000)))
    return [{"agent": t["agent_id"], "date": t["date"], "slot": t["slot"], "ticker": t["ticker"],
             "shares": _r(t["shares"]), "side": _side(t["shares"]), "fill_price": _r(t["fill_price"], 4),
             "value": _r(t["value"], 2), "commission": _r(t["commission"], 4),
             "slippage": _r(t["slippage_cost"], 4), "realized_pnl": _r(t["realized_pnl"], 2),
             "reason": t["reason"], "note": t["note"]} for t in conn.execute(sql, params)]


def _side(shares: float) -> str:
    return "buy" if shares > 0 else "sell"


def cost_totals(conn, agent_id: str) -> dict:
    t = conn.execute("SELECT COALESCE(SUM(commission),0), COALESCE(SUM(slippage_cost),0) FROM trades "
                     "WHERE agent_id=?", (agent_id,)).fetchone()
    b = conn.execute("SELECT COALESCE(SUM(amount),0) FROM borrow_fees WHERE agent_id=?", (agent_id,)).fetchone()[0]
    a = conn.execute("SELECT COALESCE(SUM(cost_usd),0), COALESCE(SUM(input_tokens+cached_tokens+cache_write_tokens),0), "
                     "COALESCE(SUM(output_tokens),0) FROM agent_turns WHERE agent_id=?", (agent_id,)).fetchone()
    return {"commissions": _r(t[0], 2), "slippage": _r(t[1], 2), "borrow_fees": _r(b, 2),
            "api_cost_usd": _r(a[0], 2), "input_tokens": a[1], "output_tokens": a[2]}


# --------------------------------------------------------------------------
# Weekly timeline, research, costs
# --------------------------------------------------------------------------

def _code_for(conn, agent_id: str, slot: int, decision_date: str) -> str | None:
    row = conn.execute("SELECT code FROM strategy_code WHERE agent_id=? AND slot=? AND saved_date<=? "
                       "ORDER BY id DESC LIMIT 1", (agent_id, slot, decision_date)).fetchone()
    return row["code"] if row else None


def weeks(conn) -> list[dict]:
    out = []
    for w in conn.execute("SELECT * FROM weeks ORDER BY week_no DESC").fetchall():
        revealed = w["status"] in ("scored", "skipped")   # skipped weeks never traded
        agents = {}
        for t in conn.execute("SELECT * FROM agent_turns WHERE week_no=? ORDER BY agent_id", (w["week_no"],)):
            score = conn.execute("SELECT * FROM scores WHERE week_no=? AND agent_id=?",
                                 (w["week_no"], t["agent_id"])).fetchone()
            entry = {"status": t["status"], "attempts": t["attempts"],
                     "weekly_return": _r(score["weekly_return"]) if score else None,
                     "point": bool(score["point"]) if score else None, "hidden": not revealed}
            if revealed:
                d = json.loads(t["decision_json"]) if t["decision_json"] else None
                entry["error"] = t["error"]
                if d:
                    for s in d.get("slots", []):
                        if s.get("type") == "code":
                            s["code"] = _code_for(conn, t["agent_id"], s.get("slot"), w["decision_date"])
                    entry.update(market_view=d.get("market_view"), slots=d.get("slots", []),
                                 notes_to_self=d.get("notes_to_self"))
            agents[t["agent_id"]] = entry
        bonus = conn.execute("SELECT * FROM bonuses WHERE period_end_week=?", (w["week_no"],)).fetchone()
        out.append({"week": w["week_no"], "decision_date": w["decision_date"], "exec_date": w["exec_date"],
                    "status": w["status"], "simulated": bool(w["simulate"]), "agents": agents,
                    "bonus_winner": bonus["winner"] if bonus else None})
    return out


def research(conn) -> list[dict]:
    """Research activity per week and agent: tool usage, searches, submits."""
    out = []
    for w in conn.execute("SELECT week_no, decision_date FROM weeks ORDER BY week_no DESC").fetchall():
        per_agent = {}
        for t in conn.execute("SELECT * FROM agent_turns WHERE week_no=?", (w["week_no"],)):
            tools = {r["tool"]: r["n"] for r in conn.execute(
                "SELECT tool, COUNT(*) AS n FROM tool_calls WHERE agent_id=? AND decision_date=? GROUP BY tool "
                "ORDER BY n DESC", (t["agent_id"], w["decision_date"]))}
            per_agent[t["agent_id"]] = {
                "status": t["status"], "tool_calls": t["tool_calls"], "web_searches": t["web_searches"],
                "submit_attempts": t["attempts"], "model_calls": t["model_calls"], "by_tool": tools,
                "input_tokens": t["input_tokens"] + t["cached_tokens"] + t["cache_write_tokens"],
                "output_tokens": t["output_tokens"], "api_cost_usd": _r(t["cost_usd"], 4)}
        out.append({"week": w["week_no"], "decision_date": w["decision_date"], "agents": per_agent})
    return out


def costs(conn) -> dict:
    weekly = []
    for w in conn.execute("SELECT * FROM weeks ORDER BY week_no").fetchall():
        nxt = conn.execute("SELECT exec_date FROM weeks WHERE week_no=?", (w["week_no"] + 1,)).fetchone()
        end = nxt["exec_date"] if nxt else "9999-12-31"
        row = {"week": w["week_no"], "agents": {}}
        for a in ledger.agent_ids(conn):
            t = conn.execute("SELECT COALESCE(SUM(commission),0), COALESCE(SUM(slippage_cost),0) FROM trades "
                             "WHERE agent_id=? AND date>=? AND date<?", (a, w["exec_date"], end)).fetchone()
            b = conn.execute("SELECT COALESCE(SUM(amount),0) FROM borrow_fees WHERE agent_id=? AND date>=? AND date<?",
                             (a, w["exec_date"], end)).fetchone()[0]
            api = conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM agent_turns WHERE agent_id=? AND week_no=?",
                               (a, w["week_no"])).fetchone()[0]
            row["agents"][a] = {"commissions": _r(t[0], 2), "slippage": _r(t[1], 2), "borrow_fees": _r(b, 2),
                                "api_cost_usd": _r(api, 4)}
        weekly.append(row)
    return {"totals": {a: cost_totals(conn, a) for a in ledger.agent_ids(conn)}, "weekly": weekly}


# --------------------------------------------------------------------------
# Alpaca paper mirror
# --------------------------------------------------------------------------

def mirror_overview(conn) -> dict:
    """What was sent to Alpaca, and for whom. In the combined mode one paper
    account holds every agent's share; `holdings` splits it per agent."""
    from .. import mirror
    run = conn.execute("SELECT * FROM mirror_runs ORDER BY id DESC LIMIT 1").fetchone()
    holdings: dict[str, dict] = {}
    for r in conn.execute("SELECT agent_id, ticker, qty FROM mirror_holdings ORDER BY ticker"):
        holdings.setdefault(r["ticker"], {})[r["agent_id"]] = _r(r["qty"])
    orders = []
    for r in conn.execute("SELECT * FROM mirror_log ORDER BY id DESC LIMIT 200"):
        qty = json.loads(r["orders"]) if r["orders"] and r["orders"].startswith("[") else []
        if not qty and not r["note"]:
            continue
        orders.append({"date": r["date"], "agent": r["leader"], "ticker": r["ticker"],
                       "qty": _r(sum(qty)) if qty else 0.0, "side": "buy" if sum(qty) > 0 else "sell" if qty else "",
                       "target_qty": _r(r["target_qty"]), "weight": _r(r["virtual_weight"]),
                       "order_ids": [o for o in (r["order_ids"] or "").split(",") if o], "note": r["note"]})
    return {"mode": mirror.mode(),
            "last_sync": dict(run) if run else None,
            "holdings": [{"ticker": t, "by_agent": by, "total": _r(sum(v or 0 for v in by.values()))}
                         for t, by in sorted(holdings.items())],
            "orders": orders}


# --------------------------------------------------------------------------
# Live values (market hours)
# --------------------------------------------------------------------------

def _chain_index(conn, agent_id: str) -> float:
    """Deposit-free performance index after the latest snapshot."""
    pts = _performance(conn, agent_id)
    return pts[-1]["index"] if pts else 1.0


def live_snapshot(conn, live: dict) -> dict:
    """Value every agent at live prices. `live` is LivePrices.state().
    Tickers without a live quote use today's traded price or the last close."""
    from datetime import datetime
    from ..prices import ET, price_on_or_before
    out = {"enabled": live.get("enabled", False), "market_open": live.get("market_open", False),
           "updated": live.get("updated"), "feed": live.get("feed"), "error": live.get("error"), "agents": {}}
    if not out["market_open"] or not live.get("quotes"):
        return out
    quotes, today = live["quotes"], datetime.now(ET).date().isoformat()

    def px(t):
        if t in quotes:
            return quotes[t]
        p = get_price(conn, t, today, "open") or (price_on_or_before(conn, t, today) or (None, None))[1]
        return p

    for a in ledger.agent_ids(conn):
        cash = sum(ledger.slot_cash(conn, a).values())
        positions, value = [], 0.0
        for p in ledger.positions(conn, a):
            last = px(p["ticker"])
            if last is None:
                continue
            v = p["shares"] * last
            value += v
            positions.append({"slot": p["slot"], "ticker": p["ticker"], "last": _r(last, 4), "value": _r(v, 2),
                              "unrealized_pnl": _r((last - p["avg_price"]) * p["shares"], 2)})
        equity = cash + value
        base = ledger.latest_snapshot(conn, a)
        change = equity / base["equity"] - 1 if base and base["equity"] > 0 else None
        since = _chain_index(conn, a) * (1 + change) - 1 if change is not None else None
        out["agents"][a] = {"equity": _r(equity, 2), "change_since_last_snapshot": _r(change),
                            "since_snapshot": base["date"] if base else None,
                            "return_since_start": _r(since), "positions": positions}
    start = _first_trade_date(conn)
    base_spy = get_price(conn, "SPY", start, "open") if start else None
    out["spy_return"] = _r(quotes["SPY"] / base_spy - 1) if base_spy and "SPY" in quotes else None
    for a in out["agents"].values():
        if a["return_since_start"] is not None and out["spy_return"] is not None:
            a["vs_spy"] = _r(a["return_since_start"] - out["spy_return"])
    return out
