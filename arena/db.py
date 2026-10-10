"""SQLite storage.

The SQL is kept plain (no SQLite-only tricks beyond INSERT OR REPLACE /
ON CONFLICT, which Postgres also supports) so swapping to Postgres later is
mostly a matter of changing `connect()`.

Every function that changes money runs inside `transaction()`, so a crash
halfway through a rebalance leaves the DB exactly as it was before.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    ticker  TEXT NOT NULL,
    date    TEXT NOT NULL,          -- YYYY-MM-DD (US/Eastern trading date)
    open    REAL NOT NULL,
    high    REAL NOT NULL,
    low     REAL NOT NULL,
    close   REAL NOT NULL,
    volume  REAL NOT NULL,
    PRIMARY KEY (ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(date);

-- Opening prices captured right after the open (from the first minutes of
-- trading), so orders can execute at the open before the day's full bar
-- exists. Once the daily bar is stored, it takes precedence.
CREATE TABLE IF NOT EXISTS opening_prices (
    ticker  TEXT NOT NULL,
    date    TEXT NOT NULL,
    open    REAL NOT NULL,
    source  TEXT NOT NULL,          -- e.g. 'alpaca-sip-1min'
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS calendar (
    date        TEXT PRIMARY KEY,   -- trading day
    open_time   TEXT NOT NULL,      -- HH:MM Eastern
    close_time  TEXT NOT NULL       -- HH:MM Eastern (13:00 on half days)
);

-- Global, monotonically increasing counter. Snapshots, trades, deposits and
-- fees all take a number from it so we always know the exact order in which
-- money moved, even when several events share the same date.
CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    agent_id    TEXT PRIMARY KEY,
    model       TEXT,
    created_at  TEXT NOT NULL
);

-- Slot 0 is the agent's unallocated cash reserve; slots 1..5 are strategies.
CREATE TABLE IF NOT EXISTS slots (
    agent_id    TEXT NOT NULL,
    slot        INTEGER NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    type        TEXT NOT NULL DEFAULT 'manual',   -- manual | code | reserve
    allocation  REAL NOT NULL DEFAULT 0,
    cash        REAL NOT NULL DEFAULT 0,
    code        TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT,
    PRIMARY KEY (agent_id, slot)
);

-- shares < 0 means short. avg_price is the average entry price of the
-- current position (used for P&L display, never for equity).
CREATE TABLE IF NOT EXISTS positions (
    agent_id   TEXT NOT NULL,
    slot       INTEGER NOT NULL,
    ticker     TEXT NOT NULL,
    shares     REAL NOT NULL,
    avg_price  REAL NOT NULL,
    -- For shorts: borrow fee has been charged up to and including this date.
    -- Tracking it (instead of "charge one day per run") keeps fees right
    -- across weekends, holidays, and missed daily runs.
    borrow_paid_through TEXT,
    PRIMARY KEY (agent_id, slot, ticker)
);

CREATE TABLE IF NOT EXISTS risk_orders (
    agent_id     TEXT NOT NULL,
    slot         INTEGER NOT NULL,
    ticker       TEXT NOT NULL,
    stop_loss    REAL,
    take_profit  REAL,
    PRIMARY KEY (agent_id, slot, ticker)
);

-- Orders created on one day and executed at a later open
-- (stop-loss, take-profit, forced cover, and in phase 3 the weekly rebalance).
CREATE TABLE IF NOT EXISTS pending_orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    TEXT NOT NULL,
    created_date TEXT NOT NULL,
    exec_date   TEXT NOT NULL,
    kind        TEXT NOT NULL,      -- stop_loss | take_profit | forced_cover | rebalance
    slot        INTEGER,
    ticker      TEXT,
    shares      REAL,               -- signed share change to apply (None for rebalance)
    payload     TEXT,               -- JSON for rebalance targets / extra info
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | done | skipped
    note        TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    seq           INTEGER NOT NULL,
    agent_id      TEXT NOT NULL,
    slot          INTEGER NOT NULL,
    date          TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    shares        REAL NOT NULL,     -- signed: + buy / cover, - sell / short
    ref_price     REAL NOT NULL,     -- open price before slippage
    fill_price    REAL NOT NULL,
    value         REAL NOT NULL,     -- |shares| * fill_price
    commission    REAL NOT NULL,
    slippage_cost REAL NOT NULL,
    realized_pnl  REAL NOT NULL,
    reason        TEXT NOT NULL,     -- rebalance | stop_loss | take_profit | forced_cover
    note          TEXT               -- why: the agent's own words, or the engine's explanation
);

CREATE TABLE IF NOT EXISTS deposits (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    seq       INTEGER NOT NULL,
    agent_id  TEXT NOT NULL,
    date      TEXT NOT NULL,
    amount    REAL NOT NULL,
    note      TEXT
);

CREATE TABLE IF NOT EXISTS borrow_fees (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    seq       INTEGER NOT NULL,
    agent_id  TEXT NOT NULL,
    slot      INTEGER NOT NULL,
    date      TEXT NOT NULL,
    ticker    TEXT NOT NULL,
    short_value REAL NOT NULL,
    days      INTEGER NOT NULL,
    amount    REAL NOT NULL
);

-- Splits (and dividends, because prices are total-return adjusted) rescale
-- history. When that happens we rescale held shares so position value is
-- unchanged; this table explains every such rescale.
CREATE TABLE IF NOT EXISTS corporate_actions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    seq       INTEGER NOT NULL,
    date      TEXT NOT NULL,
    ticker    TEXT NOT NULL,
    factor    REAL NOT NULL,         -- new_price / old_price for past bars
    note      TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    seq         INTEGER NOT NULL,
    agent_id    TEXT NOT NULL,
    date        TEXT NOT NULL,
    kind        TEXT NOT NULL,       -- post_trade | mark | daily
    price_field TEXT NOT NULL,       -- open | close
    equity      REAL NOT NULL,
    cash        REAL NOT NULL,
    long_value  REAL NOT NULL,
    short_value REAL NOT NULL,       -- positive number
    slots_json  TEXT NOT NULL        -- per-slot equity breakdown
);
CREATE INDEX IF NOT EXISTS idx_snap_agent ON snapshots(agent_id, seq);

-- Strategy code saved by agents for their code slots (every version kept,
-- so the dashboard can show what ran in each week).
CREATE TABLE IF NOT EXISTS strategy_code (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    TEXT NOT NULL,
    slot        INTEGER NOT NULL,
    saved_date  TEXT NOT NULL,     -- decision date it was saved for
    code        TEXT NOT NULL,
    test_weights TEXT              -- JSON output from the validation run
);

-- Every research tool call (phase 3 adds full message logs).
CREATE TABLE IF NOT EXISTS tool_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    TEXT NOT NULL,
    decision_date TEXT NOT NULL,
    tool        TEXT NOT NULL,
    args_json   TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    result_summary TEXT,
    duration_ms INTEGER NOT NULL
);

-- ---------------------------------------------------------------- league
CREATE TABLE IF NOT EXISTS league_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Week N: decided with data up to decision_date (Friday close), traded at
-- exec_date's open, scored at week N+1's exec open.
CREATE TABLE IF NOT EXISTS weeks (
    week_no       INTEGER PRIMARY KEY,
    decision_date TEXT NOT NULL UNIQUE,
    exec_date     TEXT NOT NULL,
    status        TEXT NOT NULL,       -- deciding | decided | executed | scored
    simulate      INTEGER NOT NULL DEFAULT 0
);

-- One row per agent per week: the outcome of its turn.
CREATE TABLE IF NOT EXISTS agent_turns (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    week_no       INTEGER NOT NULL,
    agent_id      TEXT NOT NULL,
    model         TEXT NOT NULL,
    status        TEXT NOT NULL,       -- accepted | failed
    error         TEXT,
    decision_json TEXT,                -- the accepted decision
    attempts      INTEGER NOT NULL DEFAULT 0,
    tool_calls    INTEGER NOT NULL DEFAULT 0,
    web_searches  INTEGER NOT NULL DEFAULT 0,
    model_calls   INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    UNIQUE (week_no, agent_id)
);

-- Full transcript of each turn (briefing, model output, tool calls/results).
CREATE TABLE IF NOT EXISTS turn_messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id   INTEGER NOT NULL,
    seq       INTEGER NOT NULL,
    role      TEXT NOT NULL,           -- system | user | assistant | tool
    content   TEXT NOT NULL            -- JSON
);

-- Every submit_decision attempt, valid or not.
CREATE TABLE IF NOT EXISTS decision_attempts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id   INTEGER NOT NULL,
    attempt   INTEGER NOT NULL,
    ok        INTEGER NOT NULL,
    decision_json TEXT NOT NULL,
    errors    TEXT
);

-- Code-slot runs at decision time (errors go into the next briefing).
CREATE TABLE IF NOT EXISTS slot_runs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    week_no   INTEGER NOT NULL,
    agent_id  TEXT NOT NULL,
    slot      INTEGER NOT NULL,
    ok        INTEGER NOT NULL,
    weights_json TEXT,
    error     TEXT
);

-- Weekly strategy reports: written by each agent after a week is scored.
-- report_json = the agent's own words per slot + the engine's numbers
-- (filled in from this database, never taken from the agent).
CREATE TABLE IF NOT EXISTS strategy_reports (
    week_no       INTEGER NOT NULL,
    agent_id      TEXT NOT NULL,
    status        TEXT NOT NULL,       -- running | accepted | failed
    model         TEXT,
    report_json   TEXT,
    report_text   TEXT,
    error         TEXT,
    tool_calls    INTEGER NOT NULL DEFAULT 0,
    web_searches  INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    PRIMARY KEY (week_no, agent_id)
);

-- The S&P 500 list in use (refresh-universe). Empty = the snapshot built
-- into universe.py. removed_on is set when a ticker leaves the index:
-- positions in it can be held or reduced, never increased.
CREATE TABLE IF NOT EXISTS universe_members (
    ticker      TEXT PRIMARY KEY,
    sector      TEXT NOT NULL,
    name        TEXT,
    added_on    TEXT NOT NULL,
    removed_on  TEXT
);

CREATE TABLE IF NOT EXISTS scores (
    week_no        INTEGER NOT NULL,
    agent_id       TEXT NOT NULL,
    weekly_return  REAL NOT NULL,
    point          INTEGER NOT NULL,
    start_equity   REAL NOT NULL,
    end_equity     REAL NOT NULL,
    PRIMARY KEY (week_no, agent_id)
);

CREATE TABLE IF NOT EXISTS slot_scores (
    week_no   INTEGER NOT NULL,
    agent_id  TEXT NOT NULL,
    slot      INTEGER NOT NULL,
    name      TEXT,
    weekly_return REAL NOT NULL,
    PRIMARY KEY (week_no, agent_id, slot)
);

CREATE TABLE IF NOT EXISTS bonuses (
    period_end_week INTEGER PRIMARY KEY,
    winner     TEXT,                  -- NULL on a tie
    amount     REAL NOT NULL,
    returns_json TEXT NOT NULL
);

-- Scheduler: one row per job run (see scheduler.py).
CREATE TABLE IF NOT EXISTS job_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job         TEXT NOT NULL,
    key         TEXT NOT NULL,       -- the date (or week) the run is for
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    exit_code   INTEGER,
    output_tail TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_runs ON job_runs(job, key);

-- Paper-account mirror: every order and every difference from the league (see mirror.py).
CREATE TABLE IF NOT EXISTS mirror_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    leader      TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    virtual_weight REAL NOT NULL,   -- leader's net weight in the league
    ideal_qty   REAL NOT NULL,      -- shares that weight means for the paper account
    target_qty  REAL NOT NULL,      -- what we actually aim for (after broker rules)
    current_qty REAL NOT NULL,
    orders      TEXT NOT NULL,      -- signed quantities submitted, e.g. "[-3.5, -2]"
    order_ids   TEXT,
    note        TEXT
);

-- Combined mirror mode: which part of the shared paper account belongs to
-- which agent (Alpaca itself only knows the total per ticker).
CREATE TABLE IF NOT EXISTS mirror_holdings (
    agent_id  TEXT NOT NULL,
    ticker    TEXT NOT NULL,
    qty       REAL NOT NULL,
    PRIMARY KEY (agent_id, ticker)
);

CREATE TABLE IF NOT EXISTS mirror_runs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL DEFAULT (datetime('now')),
    date      TEXT NOT NULL,
    mode      TEXT NOT NULL,
    equity    REAL NOT NULL,
    orders    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL DEFAULT (datetime('now')),
    level     TEXT NOT NULL,
    source    TEXT NOT NULL,
    message   TEXT NOT NULL
);
"""


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None = autocommit; we manage transactions ourselves
    # with SAVEPOINTs so they can nest (a rebalance inside a weekly cycle).
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    from . import universe
    universe.load(conn)        # the S&P 500 list this league uses (refresh-universe)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a database made by an older version up to date. CREATE TABLE IF
    NOT EXISTS never changes an existing table, so new columns are added here."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
    if "note" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN note TEXT")
    from .learning import schema as learning_schema
    learning_schema.migrate(conn)


_savepoint_id = 0


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """All-or-nothing block. Nestable: inner blocks become savepoints."""
    global _savepoint_id
    _savepoint_id += 1
    name = f"sp_{_savepoint_id}"
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield conn
    except BaseException:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise
    else:
        conn.execute(f"RELEASE {name}")


class DryRunRollback(Exception):
    """Raised internally to discard everything a --dry-run did."""


@contextmanager
def dry_run_guard(conn: sqlite3.Connection, dry_run: bool) -> Iterator[None]:
    """Run a block normally, or run it and then roll everything back."""
    if not dry_run:
        with transaction(conn):
            yield
        return
    try:
        with transaction(conn):
            yield
            raise DryRunRollback()
    except DryRunRollback:
        pass


def next_seq(conn: sqlite3.Connection) -> int:
    conn.execute(
        "INSERT INTO counters(name, value) VALUES ('seq', 1) "
        "ON CONFLICT(name) DO UPDATE SET value = value + 1"
    )
    return conn.execute("SELECT value FROM counters WHERE name='seq'").fetchone()[0]


def log_event(conn: sqlite3.Connection, level: str, source: str, message: str) -> None:
    conn.execute(
        "INSERT INTO events_log(level, source, message) VALUES (?,?,?)",
        (level, source, message),
    )
