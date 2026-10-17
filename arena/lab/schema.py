"""The lab's tables in the league database (arena.db).

Created by lab commands only (migrate() below), never by the live league's
own connect(): until a lab command runs, the live database is unchanged.
The bars themselves live in data/history.db (histdata.py).

    market_states      one SPY-only label per trading day (history + live)
    backtest_runs      every backtest ever run: who, what, period, results.
                       Frozen once finished, never deleted, so the attempt
                       count per idea can't be cleaned up after the fact.
    backtest_trades    the round trips of each run, tagged with the market
                       state at entry
    trade_market_states   view: every live trade with the label at decision time
    strategy_by_market_state  view: per strategy and label, live and backtest
                       side by side (the learning layer's strategy_by_regime
                       view is left as it is: the live dashboard reads it)
"""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_states (
    day          TEXT PRIMARY KEY,      -- trading day (labelled at its close)
    label        TEXT CHECK (label IS NULL OR label IN
                 ('up_low', 'up_normal', 'up_high', 'down_low', 'down_normal', 'down_high')),
    inputs       TEXT NOT NULL,         -- JSON: the numbers the rule used
    source       TEXT NOT NULL,         -- history.db feed the closes came from
    computed_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backtest_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    requested_by        TEXT NOT NULL,      -- human | engine | agent:<id>
    idea_key            TEXT NOT NULL,      -- one idea = one key; attempts are counted per key
    attempt_no          INTEGER NOT NULL,   -- this run is attempt n of the idea
    strategy_name       TEXT NOT NULL,
    strategy_version_id INTEGER,            -- strategy_versions.id for a league strategy
    code_sha256         TEXT,               -- the strategy code that ran
    params              TEXT NOT NULL DEFAULT '{}',
    purpose             TEXT NOT NULL CHECK (purpose IN ('design', 'walk_forward', 'holdout', 'check')),
    period_start        TEXT,
    period_end          TEXT,
    status              TEXT NOT NULL CHECK (status IN ('running', 'ok', 'failed', 'refused')),
    results             TEXT,               -- JSON metrics
    error               TEXT,
    settings            TEXT,               -- JSON: costs, limits, windows used
    data_note           TEXT,               -- the survivorship caveat shown with the results
    created_at          TEXT NOT NULL,
    finished_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_backtest_runs_idea ON backtest_runs(idea_key, id);
-- The locked holdout is used once per idea.
CREATE UNIQUE INDEX IF NOT EXISTS one_holdout_per_idea ON backtest_runs(idea_key)
    WHERE purpose = 'holdout' AND status IN ('running', 'ok');
CREATE TRIGGER IF NOT EXISTS backtest_runs_frozen BEFORE UPDATE ON backtest_runs
WHEN OLD.status != 'running'
BEGIN SELECT RAISE(ABORT, 'a finished backtest run is frozen'); END;
CREATE TRIGGER IF NOT EXISTS backtest_runs_no_delete BEFORE DELETE ON backtest_runs
BEGIN SELECT RAISE(ABORT, 'backtest runs are never deleted'); END;

CREATE TABLE IF NOT EXISTS backtest_trades (
    run_id        INTEGER NOT NULL REFERENCES backtest_runs(id),
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL CHECK (side IN ('long', 'short')),
    entry_day     TEXT NOT NULL,
    entry_price   REAL NOT NULL,
    exit_day      TEXT,                 -- NULL = still open at the end of the run
    exit_price    REAL,
    shares        REAL NOT NULL,
    pnl           REAL,                 -- dollars, after costs
    pnl_pct       REAL,                 -- percent of the entry value
    exit_reason   TEXT,                 -- rebalance | stop | end
    entry_state   TEXT                  -- market state at the decision before entry
);
CREATE INDEX IF NOT EXISTS idx_backtest_trades_run ON backtest_trades(run_id);

CREATE VIEW IF NOT EXISTS trade_market_states AS
SELECT t.id AS trade_id, t.agent_id, t.slot, t.date, t.ticker, t.reason,
       (SELECT m.label FROM market_states m WHERE m.day < t.date ORDER BY m.day DESC LIMIT 1) AS market_state
FROM trades t;

CREATE VIEW IF NOT EXISTS strategy_by_market_state AS
SELECT 'live'                                  AS source,
       d.agent_id                              AS agent,
       s.name                                  AS strategy,
       (SELECT m.label FROM market_states m
         WHERE (d.checkpoint = 'risk_exit' AND m.day < substr(d.decided_at, 1, 10))
            OR (d.checkpoint != 'risk_exit' AND m.day <= substr(d.decided_at, 1, 10))
         ORDER BY m.day DESC LIMIT 1)          AS market_state,
       count(*)                                AS trades,
       round(avg(o.pnl_1w_pct), 3)             AS avg_pct,
       round(avg(CASE WHEN o.pnl_1w_pct > 0 THEN 1.0 ELSE 0.0 END) * 100, 1) AS win_rate_pct
FROM decisions d
JOIN outcomes o           ON o.decision_id = d.id
JOIN strategy_versions sv ON sv.id = d.strategy_version_id
JOIN strategies s         ON s.id = sv.strategy_id
WHERE d.action <> 'hold' AND d.shadow = 0 AND o.pnl_1w_pct IS NOT NULL
GROUP BY d.agent_id, s.name, market_state
UNION ALL
SELECT 'backtest:' || r.purpose                AS source,
       r.requested_by                          AS agent,
       r.strategy_name                         AS strategy,
       bt.entry_state                          AS market_state,
       count(*)                                AS trades,
       round(avg(bt.pnl_pct), 3)               AS avg_pct,
       round(avg(CASE WHEN bt.pnl > 0 THEN 1.0 ELSE 0.0 END) * 100, 1) AS win_rate_pct
FROM backtest_trades bt JOIN backtest_runs r ON r.id = bt.run_id
WHERE r.status = 'ok' AND bt.exit_day IS NOT NULL
GROUP BY r.purpose, r.requested_by, r.strategy_name, bt.entry_state;
"""


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
