"""Read-only SQLite access. Identifiers originate exclusively in schema metadata."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException


@contextmanager
def open_ro():
    path = Path(os.getenv("ARENA_DB_PATH", "/app/data/arena.db")).absolute()
    # Streaming iterators can resume on different worker threads; they run serially.
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        yield conn
    finally:
        conn.close()


def quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def tables(conn):
    return [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_schema WHERE type = 'table' ORDER BY name")
        if not row[0].startswith("sqlite_")]


def schema(conn, name):
    actual = next((table for table in tables(conn) if table == name), None)
    if actual is None:
        raise HTTPException(404, "Unknown table")
    return actual, list(conn.execute(f"PRAGMA table_info({quote(actual)})"))


def view(conn, name, sort, direction, search):
    table, info = schema(conn, name)
    columns = [col[1] for col in info]
    if sort is not None and sort not in columns:
        raise HTTPException(400, "Unknown sort column")
    if direction not in ("asc", "desc"):
        raise HTTPException(400, "Invalid sort direction")
    # Hidden rowid aliases may be shadowed by real columns.
    rowid = next((s for s in ("rowid", "_rowid_", "oid") if s not in
                  {c.lower() for c in columns}), None)
    if rowid:
        try:
            conn.execute(f"SELECT {rowid} FROM {quote(table)} LIMIT 0")
        except sqlite3.OperationalError:
            rowid = None
    fallback = next((c for c in columns if any(
        part in c.lower() for part in ("created", "timestamp", "date")) or
        c.lower() == "ts"), None)
    order = quote(sort or fallback) if sort or fallback else rowid
    if order is None:
        order = ", ".join(quote(c[1]) for c in info if c[5])
    text = [c[1] for c in info if any(t in c[2].upper() for t in
                                     ("TEXT", "CHAR", "CLOB")) or not c[2]]
    where, args = "", []
    if search:
        where = " WHERE " + (" OR ".join(f"{quote(c)} LIKE ?" for c in text) or "0")
        args = [f"%{search}%"] * len(text)
    base = f" FROM {quote(table)}{where}"
    ordering = f" ORDER BY {order} {direction}"
    if rowid and order != rowid:
        ordering += f", {rowid} DESC"
    return columns, base, ordering, args, rowid
