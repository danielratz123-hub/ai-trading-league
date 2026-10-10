"""Tables of the data, review and learning layer, in the league's SQLite file.

Adapted from the PostgreSQL/TimescaleDB schema.sql that came with the spec
(LEARNING.md lists every difference and why). The three layers:

  1. MARKET DATA  bars_1m, daily_snapshots, market_context
  2. DECISIONS    strategies, strategy_versions, decisions (+ decision_inputs),
                  trades.decision_id, portfolio_snapshots
  3. LEARNING     outcomes (+ outcomes_history), lessons, reports (a view over
                  the existing strategy_reports), weekly_scores, shadow_runs

Golden rule (anti-lookahead): a decision row is FROZEN at decision time:
triggers below make UPDATE and DELETE on `decisions` fail. Outcomes are
written later by the grader; every version of an outcome is kept in
outcomes_history so a review "as of" time T reads exactly the outcomes that
existed before T (see grader.outcomes_as_of).
"""
from __future__ import annotations

import sqlite3

SCHEMA = """
-- ------------------------------------------------------------ league structure
-- One row per strategy an agent runs in a slot (1-5). The slot's money stays
-- in the existing `slots` table (the ledger); this is the strategy's identity.
CREATE TABLE IF NOT EXISTS strategies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id      TEXT NOT NULL REFERENCES agents(agent_id),
    slot          INTEGER NOT NULL CHECK (slot BETWEEN 1 AND 5),
    name          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'retired')),
    created_at    TEXT NOT NULL,
    created_week  INTEGER,
    retired_at    TEXT,
    retired_week  INTEGER,
    emergency_flagged_at TEXT,       -- set when the drawdown limit is hit; cleared by a rewrite
    emergency_note TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_per_slot ON strategies(agent_id, slot) WHERE status = 'active';

-- Every change of a strategy's rules or params is a new version.
-- role: champion = the version that trades; challenger = a proposed rewrite
-- running in shadow (no capital); superseded / dropped = history.
CREATE TABLE IF NOT EXISTS strategy_versions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id   INTEGER NOT NULL REFERENCES strategies(id),
    version       INTEGER NOT NULL,
    rules_text    TEXT NOT NULL,
    params        TEXT NOT NULL DEFAULT '{}',   -- JSON: stop_loss_pct, position_max_pct
    change_kind   TEXT NOT NULL CHECK (change_kind IN
                  ('initial', 'new_strategy', 'param_tune', 'emergency_rewrite', 'weekly_rewrite', 'promoted')),
    change_reason TEXT,
    hypothesis    TEXT,
    role          TEXT NOT NULL CHECK (role IN ('champion', 'challenger', 'superseded', 'dropped')),
    created_at    TEXT NOT NULL,
    created_week  INTEGER,
    UNIQUE (strategy_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_champion ON strategy_versions(strategy_id) WHERE role = 'champion';
CREATE UNIQUE INDEX IF NOT EXISTS one_challenger ON strategy_versions(strategy_id) WHERE role = 'challenger';

CREATE TABLE IF NOT EXISTS watchlists (
    agent_id   TEXT NOT NULL,
    ticker     TEXT NOT NULL,
    added_week INTEGER,
    PRIMARY KEY (agent_id, ticker)
);

-- ------------------------------------------------------------ market data
CREATE TABLE IF NOT EXISTS bars_1m (
    symbol  TEXT NOT NULL,
    ts      TEXT NOT NULL,          -- bar start, UTC, 'YYYY-MM-DDTHH:MM:00Z'
    open    REAL NOT NULL,
    high    REAL NOT NULL,
    low     REAL NOT NULL,
    close   REAL NOT NULL,
    volume  INTEGER NOT NULL,
    vwap    REAL,
    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_bars_1m_ts ON bars_1m(ts);

CREATE TABLE IF NOT EXISTS daily_snapshots (
    symbol      TEXT NOT NULL,
    day         TEXT NOT NULL,
    close       REAL NOT NULL,
    volume      INTEGER NOT NULL,
    indicators  TEXT NOT NULL DEFAULT '{}',   -- JSON: rsi_14, sma_20/50/200, atr_14, avg_volume_20
    PRIMARY KEY (symbol, day)
);

-- One row per checkpoint: 'close' after each trading day (with the regime),
-- 'intraday' from each risk check (regime copied from the last close).
CREATE TABLE IF NOT EXISTS market_context (
    ts           TEXT PRIMARY KEY,        -- UTC ISO
    day          TEXT NOT NULL,           -- ET trading day
    kind         TEXT NOT NULL CHECK (kind IN ('close', 'intraday')),
    spy_change   REAL,                    -- % change of the day so far
    vix          REAL,
    vix_proxy_change REAL,                -- intraday: VIXY % change vs prior close
    sector_moves TEXT,                    -- JSON {"XLK": 1.2, ...} in %
    headlines    TEXT,                    -- JSON [{title, source, ts}]
    regime       TEXT CHECK (regime IS NULL OR regime IN ('trend_up', 'trend_down', 'choppy', 'high_vol')),
    regime_inputs TEXT                    -- JSON: the numbers the rules used
);
CREATE INDEX IF NOT EXISTS idx_market_context_day ON market_context(day);

-- ------------------------------------------------------------ decisions
-- What an agent saw, verbatim, stored once (many decisions share one input).
CREATE TABLE IF NOT EXISTS decision_inputs (
    sha256      TEXT PRIMARY KEY,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- The journal. One row every time an agent decides something, INCLUDING
-- holds. Frozen: see the triggers below.
CREATE TABLE IF NOT EXISTS decisions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id             TEXT NOT NULL REFERENCES agents(agent_id),
    strategy_version_id  INTEGER NOT NULL REFERENCES strategy_versions(id),
    decided_at           TEXT NOT NULL,      -- UTC ISO
    checkpoint           TEXT NOT NULL CHECK (checkpoint IN
                         ('weekly_rebalance', 'risk_exit', 'daily_review', 'weekly_review')),
    regime               TEXT,
    input_snapshot       TEXT NOT NULL,      -- JSON {"sha256": ..., "kind": ...} -> decision_inputs
    lessons_used         TEXT NOT NULL DEFAULT '[]',   -- JSON array of lesson ids in its prompt
    reasoning            TEXT NOT NULL,
    confidence           REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    action               TEXT NOT NULL CHECK (action IN ('buy', 'sell', 'short', 'cover', 'hold')),
    symbol               TEXT,
    target_weight        REAL,               -- fraction of the strategy's capital
    week_no              INTEGER,
    shadow               INTEGER NOT NULL DEFAULT 0,   -- 1 = a challenger's shadow position (no capital)
    fallback             INTEGER NOT NULL DEFAULT 0,   -- 1 = output failed validation twice -> forced hold
    source_ref           TEXT                -- e.g. "turn:12", "review:7"
);
CREATE INDEX IF NOT EXISTS idx_decisions_agent ON decisions(agent_id, decided_at);
CREATE INDEX IF NOT EXISTS idx_decisions_version ON decisions(strategy_version_id);
CREATE TRIGGER IF NOT EXISTS decisions_frozen_update BEFORE UPDATE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions are frozen at decision time'); END;
CREATE TRIGGER IF NOT EXISTS decisions_frozen_delete BEFORE DELETE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions are frozen at decision time'); END;

-- Equity per strategy (slot): at every risk check and at the close.
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    agent_id        TEXT NOT NULL,
    slot            INTEGER NOT NULL,
    strategy_id     INTEGER,
    ts              TEXT NOT NULL,
    cash            REAL NOT NULL,
    positions_value REAL NOT NULL,
    equity          REAL NOT NULL,
    positions       TEXT NOT NULL,       -- JSON [{symbol, qty, avg_price, price}]
    PRIMARY KEY (agent_id, slot, ts)
);

-- ------------------------------------------------------------ learning
-- Filled in LATER by the grader, never at decision time. `outcomes` holds
-- the latest grade; outcomes_history keeps every version (append-only).
CREATE TABLE IF NOT EXISTS outcomes (
    decision_id      INTEGER PRIMARY KEY REFERENCES decisions(id),
    pnl_1d_pct       REAL,
    pnl_1w_pct       REAL,
    pnl_exit_pct     REAL,               -- NULL until the position is closed
    max_drawdown_pct REAL,
    vs_spy_1w_pct    REAL,
    exit_reason      TEXT,
    evaluated_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outcomes_history (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id      INTEGER NOT NULL REFERENCES decisions(id),
    pnl_1d_pct       REAL,
    pnl_1w_pct       REAL,
    pnl_exit_pct     REAL,
    max_drawdown_pct REAL,
    vs_spy_1w_pct    REAL,
    exit_reason      TEXT,
    evaluated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outcomes_history ON outcomes_history(decision_id, evaluated_at);
CREATE TRIGGER IF NOT EXISTS outcomes_history_frozen_update BEFORE UPDATE ON outcomes_history
BEGIN SELECT RAISE(ABORT, 'outcomes_history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS outcomes_history_frozen_delete BEFORE DELETE ON outcomes_history
BEGIN SELECT RAISE(ABORT, 'outcomes_history is append-only'); END;

CREATE TABLE IF NOT EXISTS lessons (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id           TEXT NOT NULL REFERENCES agents(agent_id),
    strategy_id        INTEGER REFERENCES strategies(id),   -- NULL = general lesson
    regime             TEXT,                                -- applies in this regime (NULL = any)
    key                TEXT NOT NULL,                       -- stable id the agent reuses to confirm it
    lesson             TEXT NOT NULL,
    kind               TEXT NOT NULL CHECK (kind IN ('keep', 'stop', 'improve', 'observation')),
    evidence_decisions TEXT NOT NULL,                       -- JSON array of decision ids
    times_confirmed    INTEGER NOT NULL DEFAULT 1,          -- anti-overfit: active only at >= N
    status             TEXT NOT NULL DEFAULT 'candidate' CHECK (status IN ('candidate', 'active', 'retired')),
    source             TEXT NOT NULL DEFAULT 'review',      -- review | engine
    last_confirmed_on  TEXT,                                -- ET day; a lesson is confirmed at most once a day
    retired_reason     TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    UNIQUE (agent_id, key)
);

-- Weekly league table under the risk-adjusted formula (all three numbers shown).
CREATE TABLE IF NOT EXISTS weekly_scores (
    week_no          INTEGER NOT NULL,
    agent_id         TEXT NOT NULL,
    week_start       TEXT NOT NULL,          -- the week's execution day
    mode             TEXT NOT NULL,          -- raw_return (weeks before scoring.start_week) | risk_adjusted
    return_pct       REAL NOT NULL,
    max_drawdown_pct REAL NOT NULL,
    broke_hard_limit INTEGER NOT NULL DEFAULT 0,
    violations       TEXT NOT NULL DEFAULT '[]',
    score            REAL NOT NULL,          -- return / max(drawdown, 1%); 0 if a limit was broken
    point_won        INTEGER NOT NULL DEFAULT 0,
    bonus_added      REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (week_no, agent_id)
);

-- Champion vs challenger: a challenger version simulated for a week on
-- paper (no capital), next to the live champion.
CREATE TABLE IF NOT EXISTS shadow_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id      INTEGER NOT NULL REFERENCES strategy_versions(id),
    champion_version_id INTEGER NOT NULL REFERENCES strategy_versions(id),
    week_no         INTEGER NOT NULL,
    weights         TEXT NOT NULL,           -- JSON {ticker: weight within the slot}
    start_date      TEXT,                    -- execution day (entry at its open)
    end_date        TEXT,                    -- next execution day (exit at its open)
    return_pct      REAL,
    max_drawdown_pct REAL,
    champion_return_pct REAL,
    champion_max_drawdown_pct REAL,
    verdict         TEXT NOT NULL DEFAULT 'pending' CHECK (verdict IN ('pending', 'promoted', 'dropped')),
    note            TEXT,
    evaluated_at    TEXT,
    UNIQUE (version_id, week_no)
);

-- ------------------------------------------------------------ process logs
CREATE TABLE IF NOT EXISTS review_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL CHECK (kind IN ('daily', 'weekly', 'risk')),
    agent_id      TEXT NOT NULL,
    as_of         TEXT NOT NULL,             -- UTC ISO: the review's start time (anti-lookahead cut-off)
    day           TEXT NOT NULL,             -- ET trading day it is for
    status        TEXT NOT NULL CHECK (status IN ('running', 'ok', 'fallback', 'failed')),
    attempts      INTEGER NOT NULL DEFAULT 0,
    input_sha256  TEXT,
    output_json   TEXT,
    applied_json  TEXT,                      -- what the engine actually did
    errors        TEXT,
    dry_run       INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    started_at    TEXT NOT NULL,
    finished_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_runs ON review_runs(kind, day, agent_id);

-- Every change the engine refused: limits enforced in code, not in the prompt.
CREATE TABLE IF NOT EXISTS rejected_changes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    agent_id   TEXT NOT NULL,
    source     TEXT NOT NULL,     -- daily_review | weekly_review | risk_check | decide | order
    slot       INTEGER,
    kind       TEXT NOT NULL,     -- param | tilt | rewrite | new_strategy | lesson | order | ...
    detail     TEXT NOT NULL,     -- JSON of what was asked
    reason     TEXT NOT NULL,
    review_run_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_rejected_changes ON rejected_changes(agent_id, ts);

-- Hourly risk checks: one row per run (also when nothing fired) ...
CREATE TABLE IF NOT EXISTS risk_checks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    day         TEXT NOT NULL,
    positions   INTEGER NOT NULL,
    fired       INTEGER NOT NULL,
    woke        TEXT NOT NULL DEFAULT '[]',   -- agents woken
    note        TEXT
);
-- ... and one row per rule that fired.
CREATE TABLE IF NOT EXISTS risk_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id    INTEGER NOT NULL REFERENCES risk_checks(id),
    ts          TEXT NOT NULL,
    day         TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    slot        INTEGER,
    ticker      TEXT,
    rule        TEXT NOT NULL,    -- stop_loss | gap_alert | vix_spike | strategy_drawdown
    value       REAL,
    threshold   REAL,
    detail      TEXT,
    suppressed  INTEGER NOT NULL DEFAULT 0     -- 1 = already fired today, no wake-up
);
CREATE INDEX IF NOT EXISTS idx_risk_events ON risk_events(agent_id, rule, day);
CREATE TABLE IF NOT EXISTS risk_last_prices (
    ticker  TEXT PRIMARY KEY,
    ts      TEXT NOT NULL,
    price   REAL NOT NULL
);

-- ------------------------------------------------------------ views
-- The weekly strategy reports (worked / didn't / improve / preserve) are the
-- existing per-slot strategy_reports; this view rolls them up per agent.
CREATE VIEW IF NOT EXISTS reports AS
SELECT r.week_no                      AS week_no,
       r.agent_id                     AS agent_id,
       w.exec_date                    AS period_start,
       (SELECT MIN(w2.exec_date) FROM weeks w2 WHERE w2.week_no > r.week_no) AS period_end,
       (SELECT group_concat('Slot ' || json_extract(s.value, '$.slot') || ': ' || json_extract(s.value, '$.what_worked'), char(10))
          FROM json_each(r.report_json, '$.slots') s) AS worked,
       (SELECT group_concat('Slot ' || json_extract(s.value, '$.slot') || ': ' || json_extract(s.value, '$.what_didnt'), char(10))
          FROM json_each(r.report_json, '$.slots') s) AS didnt_work,
       (SELECT group_concat('Slot ' || json_extract(s.value, '$.slot') || ': ' || json_extract(s.value, '$.what_to_improve'), char(10))
          FROM json_each(r.report_json, '$.slots') s) AS to_improve,
       (SELECT group_concat(json_extract(p.value, '$.key') || ': ' || json_extract(p.value, '$.text'), char(10))
          FROM json_each(r.report_json, '$.slots') s, json_each(s.value, '$.what_to_preserve') p) AS to_preserve,
       json_extract(r.report_json, '$.summary') AS summary,
       r.finished_at                  AS created_at
FROM strategy_reports r JOIN weeks w ON w.week_no = r.week_no
WHERE r.status = 'accepted' AND r.report_json IS NOT NULL;

-- Which strategy earns most in which market regime (real decisions only).
CREATE VIEW IF NOT EXISTS strategy_by_regime AS
SELECT d.agent_id                         AS agent,
       s.name                             AS strategy,
       d.regime                           AS regime,
       count(*)                           AS trades,
       round(avg(o.pnl_1w_pct), 3)        AS avg_1w_pct,
       round(avg(CASE WHEN o.pnl_1w_pct > 0 THEN 1.0 ELSE 0.0 END) * 100, 1) AS win_rate_pct
FROM decisions d
JOIN outcomes o           ON o.decision_id = d.id
JOIN strategy_versions sv ON sv.id = d.strategy_version_id
JOIN strategies s         ON s.id = sv.strategy_id
WHERE d.action <> 'hold' AND d.shadow = 0 AND o.pnl_1w_pct IS NOT NULL
GROUP BY d.agent_id, s.name, d.regime;
"""


def migrate(conn: sqlite3.Connection) -> None:
    """Create the learning tables; add the one new column on an existing table.
    Safe on every connect: nothing here changes data."""
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
    if "decision_id" not in cols:
        # Which decision a fill came from (the spec's orders/fills link).
        conn.execute("ALTER TABLE trades ADD COLUMN decision_id INTEGER")
