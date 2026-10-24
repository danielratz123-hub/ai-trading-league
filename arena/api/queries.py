"""Read-only queries behind the dashboard API. Each returns plain
JSON-ready data, so they're easy to test without a web server.

Fairness rule: an agent's decisions, reasoning and code for a week are only
revealed once that week has been scored (see weeks()).
"""
from __future__ import annotations

import json
import math
import sqlite3

from .. import ledger, scoring, stats
from ..config import SETTINGS
from ..prices import get_price, latest_price_date


def _r(x, nd=6):
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return None
    return round(float(x), nd)


def display_mark(conn) -> tuple[str | None, str]:
    """(date, field) of the latest market prices the dashboard can show.

    Normally the latest daily close. On a trading day after the open (once
    the execute job has captured opening prices) and before that day's
    close arrives, it is today's open - the price the trades filled at.

    This is display only: scoring and the ledger use their own snapshots.
    Earlier versions used the date of the latest snapshot, which froze
    every value when a daily job failed, and on execution days asked for a
    close that didn't exist yet (silently showing the previous close)."""
    close_d = conn.execute("SELECT MAX(date) FROM prices WHERE ticker='SPY'").fetchone()[0]
    open_d = conn.execute("SELECT MAX(date) FROM opening_prices WHERE ticker='SPY'").fetchone()[0]
    if open_d and (close_d is None or open_d > close_d):
        return open_d, "open"
    return close_d or latest_price_date(conn), "close"


def as_of(conn) -> str | None:
    """The date every dashboard number refers to (see display_mark). Using
    one date everywhere keeps comparisons honest, e.g. an agent's return vs
    SPY over exactly the same period."""
    return display_mark(conn)[0]


def league_status(conn) -> dict:
    paused = conn.execute("SELECT value FROM league_state WHERE key='paused'").fetchone()
    week = conn.execute("SELECT * FROM weeks ORDER BY week_no DESC LIMIT 1").fetchone()
    mark_date, mark_field = display_mark(conn)
    return {
        "paused": bool(paused and paused[0] == "1"),
        "paused_agents": sorted(r[0].split(":", 1)[1] for r in conn.execute(
            "SELECT key FROM league_state WHERE key LIKE 'paused:%' AND value='1'")),
        "prices_up_to": latest_price_date(conn),
        "as_of": mark_date,
        "as_of_field": mark_field,           # "close", or "open" on a trading day before its close
        "last_daily_snapshot": conn.execute("SELECT MAX(date) FROM snapshots WHERE kind='daily'").fetchone()[0],
        "agents": [{"id": a, "model": conn.execute("SELECT model FROM agents WHERE agent_id=?", (a,)).fetchone()[0]}
                   for a in ledger.agent_ids(conn)],
        "latest_week": dict(week) if week else None,
        "starting_cash": SETTINGS.starting_cash,
        "data_feed": SETTINGS.data_feed,
        "data_feed_label": f"{SETTINGS.data_feed.upper()} feed",
        "universe": universe_info(conn),
        "web_search_soft_limit": SETTINGS.web_search_soft_limit,
        "bonus_every_weeks": SETTINGS.bonus_every_weeks,
        "bonus_amount": SETTINGS.bonus_amount,
    }


# --------------------------------------------------------------------------
# Performance
# --------------------------------------------------------------------------


def live_stats(conn: sqlite3.Connection) -> dict:
    """SELECT-only LIVE stats. Curves are growth indices, excluding cash flows.

    Slot transfers are inferred from snapshot cash and intervening fills,
    including learning-layer tilts without a post-trade snapshot. Transfers
    use a start-of-snapshot-interval convention. Slot identity includes retired/reused
    slots, not strategy versions. Closed trades are full position episodes,
    as in the lab, including partial exits, commissions and short borrow.
    """
    end = conn.execute("SELECT MAX(date) FROM snapshots WHERE kind='daily'").fetchone()[0]
    agents = []
    for agent in ledger.agent_ids(conn):
        cutoff = conn.execute("SELECT MAX(seq) FROM snapshots WHERE agent_id=? AND kind='daily' AND date<=?",
                              (agent, end)).fetchone()[0]
        snaps = conn.execute("SELECT * FROM snapshots WHERE agent_id=? AND seq<=? ORDER BY seq",
                             (agent, cutoff)).fetchall()
        deps = conn.execute("SELECT seq, amount FROM deposits WHERE agent_id=? ORDER BY seq", (agent,)).fetchall()
        fills = conn.execute("SELECT * FROM trades WHERE agent_id=? AND seq<=? ORDER BY seq",
                             (agent, cutoff)).fetchall()
        # Replay fills: pnl=0 can mean an opening fill OR a breakeven close.
        events = [(r["seq"], "trade", r) for r in fills]
        events += [(r["seq"], "borrow", r) for r in conn.execute(
            "SELECT * FROM borrow_fees WHERE agent_id=? AND seq<=?", (agent, cutoff))]
        events += [(r["seq"], "adjust", r) for r in conn.execute(
            "SELECT * FROM corporate_actions WHERE seq<=?", (cutoff,))]
        positions, closed = {}, []
        for _, kind, row in sorted(events, key=lambda e: e[0]):
            if kind == "adjust":
                for (_, ticker), ep in positions.items():
                    if ticker == row["ticker"]:
                        ep["shares"] /= row["factor"]
                continue
            key = (row["slot"], row["ticker"])
            if kind == "borrow":
                if key in positions:
                    positions[key]["pnl"] -= row["amount"]
                continue
            ep = positions.get(key, {"shares": 0.0, "pnl": 0.0})
            old, delta = ep["shares"], row["shares"]
            closing = min(abs(old), abs(delta)) if old * delta < 0 else 0.0
            closing_fee = row["commission"] * closing / abs(delta) if delta else 0.0
            ep["pnl"] += row["realized_pnl"] - closing_fee
            if closing and closing >= abs(old) - ledger.DUST:
                closed.append((row["date"], row["slot"], ep["pnl"]))
                ep = {"shares": 0.0, "pnl": 0.0}
            ep["pnl"] -= row["commission"] - closing_fee
            ep["shares"] = old + delta
            if abs(ep["shares"]) < ledger.DUST:
                positions.pop(key, None)
            else:
                positions[key] = ep

        indices, curves, previous = {}, {}, None
        agent_index, agent_curve = 1.0, {}
        # Reconstruct cash at each snapshot: slot equity minus marked holdings.
        # Changes in cash not explained by fills/borrow are capital transfers.
        adjustments = conn.execute("SELECT * FROM corporate_actions ORDER BY seq").fetchall()
        cash_events = [(r["seq"], "trade", r) for r in fills]
        cash_events += [(r["seq"], "borrow", r) for r in conn.execute(
            "SELECT * FROM borrow_fees WHERE agent_id=? AND seq<=?", (agent, cutoff))]
        cash_events += [(r["seq"], "adjust", r) for r in adjustments if cutoff and r["seq"] <= cutoff]
        cash_events.sort(key=lambda e: e[0])
        event_index, quantities, previous_cash = 0, {}, {}
        funded, marks = set(), {}
        for snap in snaps:
            cash_changes = {}
            while event_index < len(cash_events) and cash_events[event_index][0] < snap["seq"]:
                _, kind, row = cash_events[event_index]
                event_index += 1
                if kind == "adjust":
                    for key in quantities:
                        if key[1] == row["ticker"]:
                            quantities[key] /= row["factor"]
                    continue
                slot = str(row["slot"])
                if kind == "trade":
                    key = (slot, row["ticker"])
                    quantities[key] = quantities.get(key, 0.0) + row["shares"]
                    change = -row["shares"] * row["fill_price"] - row["commission"]
                else:
                    change = -row["amount"]
                cash_changes[slot] = cash_changes.get(slot, 0.0) + change
            slot_eq = json.loads(snap["slots_json"])
            prev_slots = json.loads(previous["slots_json"]) if previous else {}
            added = sum(d["amount"] for d in deps
                        if (previous["seq"] if previous else 0) < d["seq"] < snap["seq"])
            base = previous["equity"] if previous else added
            if base > 0:
                agent_index *= (snap["equity"] - (added if previous else 0)) / base
            holdings = {}
            for (slot, ticker), quantity in quantities.items():
                if abs(quantity) < ledger.DUST:
                    continue
                key = (ticker, snap["date"], snap["price_field"])
                if key not in marks:
                    marks[key] = ledger.mark_price(conn, *key)[0]
                # Stored prices may have been adjusted since this snapshot.
                factor = math.prod(r["factor"] for r in adjustments
                                   if r["ticker"] == ticker and r["seq"] > snap["seq"])
                holdings[slot] = holdings.get(slot, 0.0) + quantity / factor * marks[key]
            for slot in set(slot_eq) | set(prev_slots) | set(holdings):
                value, old = slot_eq.get(slot, 0.0), prev_slots.get(slot, 0.0)
                cash = value - holdings.get(slot, 0.0)
                flow = cash - previous_cash.get(slot, 0.0) - cash_changes.get(slot, 0.0)
                # Ledger snapshots round slot equity to six decimal places.
                if abs(flow) < 1e-6:
                    flow = 0.0
                capital = old + flow
                if capital > 1e-6:
                    funded.add(slot)
                    indices[slot] = indices.get(slot, 1.0) * value / capital
                elif old > 1e-6 and flow < 0:
                    # A retired slot can be left owing its closing fill costs.
                    indices[slot] = indices.get(slot, 1.0) * (1 + value / old)
                previous_cash[slot] = cash
                if snap["kind"] == "daily" and slot in funded:
                    curves.setdefault(slot, {})[snap["date"]] = indices.get(slot, 1.0)
            if snap["kind"] == "daily":
                agent_curve[snap["date"]] = agent_index
            previous = snap
        slots = []
        for row in conn.execute("SELECT slot, name FROM slots WHERE agent_id=? AND slot!=0 ORDER BY slot", (agent,)):
            slot = row["slot"]
            slots.append({"slot": slot, "name": row["name"], "windows": stats.window_stats(
                list(curves.get(str(slot), {}).items()), [(d, p) for d, s, p in closed if s == slot],
                as_of=end, initial_equity=1.0)})
        agents.append({"agent": agent, "windows": stats.window_stats(
            list(agent_curve.items()), [(d, p) for d, _, p in closed], as_of=end, initial_equity=1.0),
            "slots": slots})
    return {"as_of": end, "equity_curve_unit": "growth_index", "agents": agents}


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


_BENCHMARK_LABELS = {
    "bench_spy": "SPY buy-and-hold",
    "bench_random": "Random picker",
}


def _agent_weekly_scores(conn, agent_id: str) -> tuple[float | None, float | None]:
    try:
        rows = conn.execute(
            "SELECT score FROM weekly_scores WHERE agent_id=? AND score IS NOT NULL ORDER BY week_no",
            (agent_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return None, None
    if not rows:
        return None, None
    return _r(sum(r[0] for r in rows)), _r(rows[-1][0])


def _benchmark_scoreboard(conn) -> list[dict]:
    from .. import benchmarks
    try:
        has_any = conn.execute("SELECT 1 FROM benchmark_scores LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return []
    if not has_any:
        return []
    out = []
    for pid in benchmarks.PLAYER_IDS:
        rows = conn.execute(
            "SELECT return_pct, score FROM benchmark_scores WHERE player_id=? ORDER BY week_no",
            (pid,),
        ).fetchall()
        rets = [r["return_pct"] / 100.0 for r in rows if r["return_pct"] is not None]
        scores = [r["score"] for r in rows if r["score"] is not None]
        cum_ret = _r(math.prod(1 + r for r in rets) - 1) if rets else None
        last_ret = _r(rows[-1]["return_pct"] / 100.0) if rows and rows[-1]["return_pct"] is not None else None
        last_score = _r(rows[-1]["score"]) if rows and rows[-1]["score"] is not None else None
        total_score = _r(sum(scores)) if scores else None
        out.append({
            "player_id": pid,
            "id": pid,
            "label": _BENCHMARK_LABELS.get(pid, pid),
            "total_score": total_score,
            "cumulative_return": cum_ret,
            "last_week_return": last_ret,
            "last_week_score": last_score,
        })
    return out


def scoreboard(conn) -> dict:
    d, fld = display_mark(conn)
    start = _first_trade_date(conn)
    spy = None
    if d and start:
        base, last = get_price(conn, "SPY", start, "open"), get_price(conn, "SPY", d, fld)
        spy = _r(last / base - 1) if base and last else None
    rows = []
    for row in scoring.scoreboard(conn, d) if d else []:
        if fld == "open":
            # Value at today's open instead of a close that doesn't exist yet.
            row = {**row, **_open_values(conn, row["agent"], d)}
        perf = _performance(conn, row["agent"])
        since_start = perf[-1]["index"] - 1 if perf else None
        total_score, last_week_score = _agent_weekly_scores(conn, row["agent"])
        rows.append({**{k: _r(v) if isinstance(v, float) else v for k, v in row.items()},
                     "return_since_start": _r(since_start),
                     "vs_spy": _r(since_start - spy) if since_start is not None and spy is not None else None,
                     "total_score": total_score,
                     "last_week_score": last_week_score})
    return {"as_of": d, "as_of_field": fld, "since": start, "spy_return": spy,
            "agents": rows, "benchmarks": _benchmark_scoreboard(conn)}


def _open_values(conn, agent_id: str, d: str) -> dict:
    """Equity and week-to-date at d's open (display only)."""
    eq = ledger.value_agent(conn, agent_id, d, "open").equity
    start = ledger.latest_snapshot(conn, agent_id, "post_trade")
    wtd = None
    if start is not None and start["equity"] > 0:
        added = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM deposits WHERE agent_id=? AND seq > ?",
                             (agent_id, start["seq"])).fetchone()[0]
        wtd = (eq - added) / start["equity"] - 1
    return {"equity": eq, "week_to_date": wtd}


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
    d, fld = display_mark(conn)
    # "last" = the latest mark (close, or today's open before the close),
    # never the entry price. value_agent falls back to an older close only
    # when a ticker has no bar for that date; those are flagged as stale.
    val = ledger.value_agent(conn, agent_id, d, fld)
    eq = val.equity
    L = SETTINGS.limits
    risk = {(r["slot"], r["ticker"]): r for r in conn.execute(
        "SELECT * FROM risk_orders WHERE agent_id=?", (agent_id,))}
    holdings = []
    slot_names = {r["slot"]: r["name"] for r in conn.execute("SELECT slot, name FROM slots WHERE agent_id=?", (agent_id,))}
    for p in ledger.positions(conn, agent_id):
        px = val.prices[p["ticker"]]
        v = p["shares"] * px
        r = risk.get((p["slot"], p["ticker"]))
        holdings.append({
            "slot": p["slot"], "strategy": slot_names.get(p["slot"]) or f"Slot {p['slot']}",
            "ticker": p["ticker"], "side": "long" if p["shares"] > 0 else "short",
            "shares": _r(p["shares"]), "avg_price": _r(p["avg_price"], 4), "last": _r(px, 4),
            "price_stale": p["ticker"] in val.stale,
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
    return {"agent": agent_id, "as_of": d, "as_of_field": fld, "equity": _r(eq, 2), "cash": _r(val.cash, 2),
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


_TURN_TABLES = ("agent_turns", "strategy_reports")      # decision turns + report turns


def cost_totals(conn, agent_id: str) -> dict:
    t = conn.execute("SELECT COALESCE(SUM(commission),0), COALESCE(SUM(slippage_cost),0) FROM trades "
                     "WHERE agent_id=?", (agent_id,)).fetchone()
    b = conn.execute("SELECT COALESCE(SUM(amount),0) FROM borrow_fees WHERE agent_id=?", (agent_id,)).fetchone()[0]
    api = inp = out = 0
    for table in _TURN_TABLES:     # report turns count too
        a = conn.execute(f"SELECT COALESCE(SUM(cost_usd),0), COALESCE(SUM(input_tokens+cached_tokens+cache_write_tokens),0), "
                         f"COALESCE(SUM(output_tokens),0) FROM {table} WHERE agent_id=?", (agent_id,)).fetchone()
        api, inp, out = api + a[0], inp + a[1], out + a[2]
    return {"commissions": _r(t[0], 2), "slippage": _r(t[1], 2), "borrow_fees": _r(b, 2),
            "api_cost_usd": _r(api, 2), "input_tokens": inp, "output_tokens": out}


def _price_key(agent_id: str, model: str | None) -> str:
    if model and model in SETTINGS.prices_per_mtok:
        return model
    return "gpt" if agent_id == "gpt" or (model or "").startswith("gpt") else "claude-opus-5-5"


def estimated_ai_cost(agent_id: str, model: str | None, inp: int, cached: int, write: int, out: int) -> float:
    """What these tokens would cost on the pay-per-token API, from the price
    table in config.py. On subscriptions you don't pay this; it shows the
    size of the usage in money terms."""
    p_in, p_cached, p_write, p_out = SETTINGS.prices_per_mtok[_price_key(agent_id, model)]
    return (inp * p_in + cached * p_cached + write * p_write + out * p_out) / 1e6


def monthly_costs(conn) -> list[dict]:
    """Per calendar month and agent: tokens and estimated AI cost (decision
    + report turns, from the turn logs) next to the trading costs."""
    months: dict[str, dict] = {}

    def slot(month, agent):
        return months.setdefault(month, {}).setdefault(agent, {
            "turns": 0, "report_turns": 0, "input_tokens": 0, "cached_tokens": 0, "cache_write_tokens": 0,
            "output_tokens": 0, "web_searches": 0, "est_ai_cost_usd": 0.0, "api_cost_usd": 0.0,
            "commissions": 0.0, "slippage": 0.0, "borrow_fees": 0.0})

    for table in _TURN_TABLES:
        for r in conn.execute(f"SELECT agent_id, model, substr(started_at, 1, 7) AS m, COUNT(*) AS n, "
                              f"SUM(input_tokens) AS i, SUM(cached_tokens) AS c, SUM(cache_write_tokens) AS w, "
                              f"SUM(output_tokens) AS o, SUM(web_searches) AS s, SUM(cost_usd) AS api "
                              f"FROM {table} GROUP BY agent_id, model, m"):
            e = slot(r["m"], r["agent_id"])
            e["report_turns" if table == "strategy_reports" else "turns"] += r["n"]
            for k, col in (("input_tokens", "i"), ("cached_tokens", "c"), ("cache_write_tokens", "w"),
                           ("output_tokens", "o"), ("web_searches", "s")):
                e[k] += r[col] or 0
            e["api_cost_usd"] += r["api"] or 0.0
            e["est_ai_cost_usd"] += estimated_ai_cost(r["agent_id"], r["model"], r["i"] or 0, r["c"] or 0,
                                                      r["w"] or 0, r["o"] or 0)
    for r in conn.execute("SELECT agent_id, substr(date, 1, 7) AS m, SUM(commission) AS c, SUM(slippage_cost) AS s "
                          "FROM trades GROUP BY agent_id, m"):
        e = slot(r["m"], r["agent_id"])
        e["commissions"], e["slippage"] = r["c"] or 0.0, r["s"] or 0.0
    for r in conn.execute("SELECT agent_id, substr(date, 1, 7) AS m, SUM(amount) AS b FROM borrow_fees "
                          "GROUP BY agent_id, m"):
        slot(r["m"], r["agent_id"])["borrow_fees"] = r["b"] or 0.0
    out = []
    for m in sorted(months, reverse=True):
        out.append({"month": m, "agents": {a: {k: (_r(v, 4) if isinstance(v, float) else v) for k, v in e.items()}
                                           for a, e in months[m].items()}})
    return out


def daily_costs(conn, days: int = 30) -> list[dict]:
    """Per UTC day and agent (newest first): turns, tokens and estimated AI
    cost, decision + report turns, from the turn logs."""
    out: dict[str, dict] = {}
    for table in _TURN_TABLES:
        for r in conn.execute(f"SELECT agent_id, model, substr(started_at, 1, 10) AS d, COUNT(*) AS n, "
                              f"SUM(input_tokens) AS i, SUM(cached_tokens) AS c, SUM(cache_write_tokens) AS w, "
                              f"SUM(output_tokens) AS o FROM {table} "
                              f"WHERE started_at >= date('now', ?) GROUP BY agent_id, model, d",
                              (f"-{days - 1} days",)):
            e = out.setdefault(r["d"], {}).setdefault(r["agent_id"], {
                "turns": 0, "tokens": 0, "output_tokens": 0, "est_ai_cost_usd": 0.0})
            e["turns"] += r["n"]
            e["tokens"] += (r["i"] or 0) + (r["c"] or 0) + (r["w"] or 0) + (r["o"] or 0)
            e["output_tokens"] += r["o"] or 0
            e["est_ai_cost_usd"] += estimated_ai_cost(r["agent_id"], r["model"], r["i"] or 0, r["c"] or 0,
                                                      r["w"] or 0, r["o"] or 0)
    return [{"day": d, "agents": {a: {**e, "est_ai_cost_usd": _r(e["est_ai_cost_usd"], 4)} for a, e in out[d].items()}}
            for d in sorted(out, reverse=True)]


def spy_week_return(conn, week_no: int) -> float | None:
    """SPY over the same window a week is scored on: the open of its
    execution day to the open of the next execution day."""
    w = conn.execute("SELECT exec_date FROM weeks WHERE week_no=?", (week_no,)).fetchone()
    nxt = conn.execute("SELECT exec_date FROM weeks WHERE week_no>? AND status IN ('executed','scored') "
                       "ORDER BY week_no LIMIT 1", (week_no,)).fetchone()
    if not w or not nxt:
        return None
    a, b = get_price(conn, "SPY", w["exec_date"], "open"), get_price(conn, "SPY", nxt["exec_date"], "open")
    return _r(b / a - 1) if a and b else None


def universe_info(conn) -> dict:
    """The S&P 500 list in use (see refresh-universe)."""
    def get(k):
        r = conn.execute("SELECT value FROM league_state WHERE key=?", (k,)).fetchone()
        return r[0] if r else None
    try:
        n = conn.execute("SELECT COUNT(*) FROM universe_members WHERE removed_on IS NULL").fetchone()[0]
    except sqlite3.OperationalError:
        n = 0
    if not n:
        from ..universe import SNAPSHOT_LABEL, _build
        return {"list_date": None, "source": SNAPSHOT_LABEL, "members": sum(1 for s in _build().values() if s != "ETF")}
    return {"list_date": get("universe_list_date"), "source": get("universe_source"), "members": n}


def reports(conn) -> list[dict]:
    """Both agents' strategy reports per week, side by side. Only for scored
    weeks (the fairness rule): a report is never shown before its week is."""
    out = []
    for w in conn.execute("SELECT week_no, decision_date, exec_date, simulate FROM weeks WHERE status='scored' "
                          "ORDER BY week_no DESC").fetchall():
        agents = {}
        for r in conn.execute("SELECT * FROM strategy_reports WHERE week_no=?", (w["week_no"],)):
            agents[r["agent_id"]] = {
                "status": r["status"], "error": r["error"],
                "report": json.loads(r["report_json"]) if r["report_json"] else None,
                "text": r["report_text"]}
        if agents:
            out.append({"week": w["week_no"], "decision_date": w["decision_date"], "exec_date": w["exec_date"],
                        "simulated": bool(w["simulate"]), "agents": agents})
    return out


def preserve_repeats(conn) -> dict[str, list[dict]]:
    from ..reports import preserve_counts
    return {a: sorted(({"key": k, **v} for k, v in preserve_counts(conn, a).items()), key=lambda x: -x["times"])
            for a in ledger.agent_ids(conn)}


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
                    "bonus_winner": bonus["winner"] if bonus else None,
                    # The benchmark over the same window the week is scored on.
                    "spy_return": spy_week_return(conn, w["week_no"]) if w["status"] == "scored" else None})
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
        out.append({"week": w["week_no"], "decision_date": w["decision_date"], "agents": per_agent,
                    "web_search_soft_limit": SETTINGS.web_search_soft_limit})
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
    return {"totals": {a: cost_totals(conn, a) for a in ledger.agent_ids(conn)}, "weekly": weekly,
            "monthly": monthly_costs(conn), "daily": daily_costs(conn),
            "daily_alert_usd": SETTINGS.daily_ai_cost_alert_usd}


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
            "sizing": mirror.sizing(),
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
           "status": live.get("status"), "label": live.get("label"), "as_of": live.get("as_of"),
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
                              "shares": _r(p["shares"]), "avg_price": _r(p["avg_price"], 4),
                              "unrealized_pnl": _r((last - p["avg_price"]) * p["shares"], 2)})
        equity = cash + value
        base = ledger.latest_snapshot(conn, a)
        change = equity / base["equity"] - 1 if base and base["equity"] > 0 else None
        since = _chain_index(conn, a) * (1 + change) - 1 if change is not None else None
        out["agents"][a] = {"equity": _r(equity, 2), "estimated": True, "cash": _r(cash, 2),
                            "change_since_last_snapshot": _r(change),
                            "since_snapshot": base["date"] if base else None,
                            "return_since_start": _r(since), "positions": positions}
    start = _first_trade_date(conn)
    base_spy = get_price(conn, "SPY", start, "open") if start else None
    out["spy_return"] = _r(quotes["SPY"] / base_spy - 1) if base_spy and "SPY" in quotes else None
    for a in out["agents"].values():
        if a["return_since_start"] is not None and out["spy_return"] is not None:
            a["vs_spy"] = _r(a["return_since_start"] - out["spy_return"])
    return out
