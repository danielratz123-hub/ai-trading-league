"""Shared setup for the learning-layer tests: a small league in the
conftest's in-memory database (calendar 2024-01-02 .. 2024-03-29)."""
from __future__ import annotations

from datetime import datetime, timezone

from arena import ledger
from arena.learning import strategies as strat
from arena.learning.review import RunnerResult
from arena.ledger import SlotTarget
from arena.prices import ET

from .conftest import DAYS, flat_prices, set_price

DECISION = "2024-01-05"      # Friday
EXEC = "2024-01-08"          # Monday
DECIDED_AT = "2024-01-06T15:00:00+00:00"   # Saturday
TICKERS = ["SPY", "AAPL", "MSFT", "XOM", "JPM", "KO", "PG"]


def prices(conn, price: float = 100.0) -> None:
    for t in TICKERS:
        flat_prices(conn, t, price)


def utc(d: str, hh: int, mm: int) -> datetime:
    return datetime.fromisoformat(d).replace(hour=hh, minute=mm, tzinfo=ET).astimezone(timezone.utc)


def minute(conn, sym: str, d: str, hh: int, mm: int, price: float) -> None:
    ts = utc(d, hh, mm).strftime("%Y-%m-%dT%H:%M:00Z")
    conn.execute("INSERT OR REPLACE INTO bars_1m(symbol, ts, open, high, low, close, volume, vwap) "
                 "VALUES (?,?,?,?,?,?,?,?)", (sym, ts, price, price, price, price, 1000, price))


def stops(weights: dict[str, float], price: float = 100.0, pct: float = 0.05) -> dict:
    return {t: {"stop_loss": price * (1 - pct) if w > 0 else price * (1 + pct)} for t, w in weights.items() if w}


def league_week1(conn, agent: str = "claude", slots: dict[int, tuple[str, float, dict]] | None = None,
                 through: str = "2024-01-12") -> dict[int, strat.Strategy]:
    """Agent + strategies, week 1 traded at EXEC's open, daily snapshots up to `through`."""
    if conn.execute("SELECT 1 FROM agents WHERE agent_id=?", (agent,)).fetchone() is None:
        ledger.create_agent(conn, agent, 1000.0, DAYS[0])
    slots = slots or {1: ("momentum", 0.5, {"AAPL": 0.2, "MSFT": 0.2, "XOM": 0.2, "JPM": 0.2, "KO": 0.2}),
                      2: ("defensive", 0.3, {"PG": 0.2, "KO": 0.2})}
    if conn.execute("SELECT 1 FROM weeks WHERE decision_date=?", (DECISION,)).fetchone() is None:
        conn.execute("INSERT INTO weeks(week_no, decision_date, exec_date, status) VALUES (1, ?, ?, 'executed')",
                     (DECISION, EXEC))
    out = {}
    for slot, (name, _, _) in slots.items():
        out[slot] = strat.in_slot(conn, agent, slot) or strat.create(
            conn, agent, slot, name, f"{name} rules: buy strength, sell weakness", None, "initial", "test", 1,
            "2024-01-05T21:00:00+00:00")
    targets = [SlotTarget(s, alloc, w, name, "manual", risk_orders=stops(w)) for s, (name, alloc, w) in slots.items()]
    ledger.snapshot(conn, agent, EXEC, "mark", "open")
    ledger.rebalance(conn, agent, EXEC, targets)
    ledger.snapshot(conn, agent, EXEC, "post_trade", "open")
    for d in DAYS:
        if EXEC <= d <= through:
            ledger.snapshot(conn, agent, d, "daily", "close")
    return out


def fixed_runner(*outputs):
    """A runner that returns the given outputs in order (None = invalid twice)."""
    calls = []
    queue = list(outputs)

    def run(agent_id, kind, prompt, inp):
        calls.append({"agent": agent_id, "kind": kind, "prompt": prompt, "inp": inp})
        out = queue.pop(0) if queue else {"reasoning": "hold", "confidence": 0.5}
        if out is None:
            return RunnerResult(None, attempts=2, errors=["confidence: a number from 0 to 1 is required."],
                                error="The agent didn't submit a valid review.")
        return RunnerResult(out, attempts=1)
    run.calls = calls
    return run


def no_runner(agent_id, kind, prompt, inp):
    raise AssertionError("the agent must not be woken")


__all__ = ["DAYS", "set_price", "league_week1", "prices", "minute", "fixed_runner", "no_runner", "utc",
           "DECISION", "EXEC", "DECIDED_AT", "stops"]
