"""Structured (JSON) logs with a run_id.

Every scheduled job gets a run_id (`job-<job_runs id>`, passed to the job's
process as ARENA_RUN_ID). Everything that job logs carries it, plus the
agent / strategy / model when a piece of code is working for one
(`with bind(agent=..., model=...)`). The scheduler also writes every line a
job prints to data/logs/<date>.jsonl with the same run_id, so one review
cycle is one filter away:

    grep '"run_id": "job-123"' data/logs/2026-10-12.jsonl
    jq 'select(.run_id == "job-123")' data/logs/2026-10-12.jsonl

JSON is used when ARENA_LOG_JSON=1 or on the server (ARENA_SERVER=1);
otherwise plain text, as before.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

FIELDS = ("run_id", "job", "agent", "strategy", "model")
_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("arena_log_ctx", default={})
_PROCESS_RUN_ID = os.environ.get("ARENA_RUN_ID") or f"proc-{uuid.uuid4().hex[:10]}"


def run_id() -> str:
    return _ctx.get().get("run_id") or _PROCESS_RUN_ID


def context() -> dict:
    return {"run_id": run_id(), "job": os.environ.get("ARENA_JOB"), **{k: v for k, v in _ctx.get().items() if v}}


@contextmanager
def bind(**fields):
    """Add fields (agent=, strategy=, model=, ...) to every log line inside the block."""
    token = _ctx.set({**_ctx.get(), **{k: v for k, v in fields.items() if v is not None}})
    try:
        yield
    finally:
        _ctx.reset(token)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
               "level": record.levelname.lower(), "logger": record.name, "msg": record.getMessage(),
               **{k: v for k, v in context().items() if v}}
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


class _ContextFilter(logging.Filter):
    """Plain-text logs get the same fields as attributes (not shown, but available)."""
    def filter(self, record):
        for k, v in context().items():
            setattr(record, k, v)
        return True


def use_json() -> bool:
    return os.environ.get("ARENA_LOG_JSON", "") == "1" or os.environ.get("ARENA_SERVER", "") == "1"


def setup(level: int = logging.WARNING) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(_ContextFilter())
    handler.setFormatter(JsonFormatter() if use_json() else
                         logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    # Structured logs keep the INFO lines (review / turn start and end), for the run_id trail.
    root.setLevel(min(level, logging.INFO) if use_json() else level)


def write_job_lines(log_dir: Path, run: str, job: str, key: str, started: datetime, output: str, exit_code: int) -> None:
    """The scheduler's JSON log of one job run: every output line, then the result."""
    path = log_dir / f"{started:%Y-%m-%d}.jsonl"
    base = {"run_id": run, "job": job, "key": key}
    with open(path, "a", encoding="utf-8") as f:
        for line in output.splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)            # a JSON log line from the job itself
                if not isinstance(rec, dict):
                    raise ValueError
                rec = {**base, **rec}
            except ValueError:
                rec = {**base, "msg": line}
            f.write(json.dumps(rec, default=str) + "\n")
        f.write(json.dumps({**base, "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                            "event": "job_finished", "exit_code": exit_code}) + "\n")
