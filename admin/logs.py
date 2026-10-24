"""Bounded, read-only error log collection for the private admin API."""
from __future__ import annotations

import heapq
import json
import sqlite3
from datetime import datetime, timezone

from . import db
from .config import load
from .masking import mask_text

SERVICES = ('all', 'league', 'agents', 'mirror/broker')
LEVELS = ('warning', 'error', 'critical')
TAIL_BYTES = 256 * 1024


def service_for(source='', agent=None):
    source = str(source).lower()
    if any(part in source for part in ('mirror', 'broker', 'alpaca', 'prices', 'reconcile')):
        return 'mirror/broker'
    if agent or any(part in source for part in ('agent', 'claude', 'gpt')) or source.startswith(
            ('decide:', 'review:', 'report:', 'login:', 'learning:', 'risk:', 'hard-limit:', 'rebalance:', 'daily:')):
        return 'agents'
    return 'league'


def entry(record, origin):
    level = record.get('level')
    if not isinstance(level, str) or level.lower() not in LEVELS:
        return None
    try:
        stamp = datetime.fromisoformat(record['ts'])
        stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
        ts = stamp.astimezone(timezone.utc).isoformat()
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    source = record.get('source') or record.get('logger') or record.get('job') or ''
    result = {'ts': ts, 'level': level.lower(), 'origin': origin,
              'service': service_for(source, record.get('agent')),
              'message': mask_text(str(record.get('message', record.get('msg', ''))))}
    for field in ('source', 'logger', 'run_id', 'job', 'agent', 'strategy', 'model', 'exc'):
        value = record.get(field)
        if isinstance(value, (str, int, float)):
            result[field] = mask_text(str(value))
    return result


def tail(path):
    """Read at most one bounded tail, discarding any partial first/last line."""
    with path.open('rb') as stream:
        stream.seek(0, 2)
        size = stream.tell()
        start = max(0, size - TAIL_BYTES)
        stream.seek(start - 1 if start else 0)
        partial = start and stream.read(1) != b'\n'
        raw = stream.read(min(size, TAIL_BYTES))
    lines = raw.splitlines()
    if partial and lines:
        lines.pop(0)
    if raw and not raw.endswith(b'\n') and lines:
        lines.pop()
    for line in lines:
        try:
            record = json.loads(line)
        except (ValueError, UnicodeDecodeError, RecursionError):
            continue
        if isinstance(record, dict):
            yield record


def collect(cfg=None, *, service='all', limit=50):
    cfg = cfg or load()
    if service not in SERVICES:
        raise ValueError('Unknown service')
    limit = max(1, min(limit, 50))
    entries, errors = [], []
    try:
        # Date-named files in arena/logs.py sort chronologically. Keep only two.
        paths = heapq.nlargest(2, (p for p in (cfg.data_dir / 'logs').glob('*.jsonl')
                                  if not p.is_symlink() and p.is_file()), key=lambda p: p.name)
        for path in paths:
            try:
                for record in reversed(list(tail(path))):
                    item = entry(record, 'jsonl')
                    if item and (service == 'all' or item['service'] == service):
                        entries.append(item)
            except OSError as exc:
                errors.append({'origin': 'jsonl', 'error': type(exc).__name__})
    except OSError as exc:
        errors.append({'origin': 'jsonl', 'error': type(exc).__name__})
    try:
        with db.open_ro() as conn:
            conn.create_function('admin_service', 1, service_for)
            rows = conn.execute(
                "SELECT ts, level, source, message FROM events_log "
                "WHERE lower(level) IN ('warning', 'error', 'critical') "
                "AND (? = 'all' OR admin_service(source) = ?) "
                "ORDER BY julianday(ts) DESC, id DESC LIMIT ?", (service, service, limit))
            for row in rows:
                item = entry(dict(row), 'events_log')
                if item:
                    entries.append(item)
    except sqlite3.Error as exc:
        errors.append({'origin': 'events_log', 'error': type(exc).__name__})
    entries.sort(key=lambda item: item['ts'], reverse=True)
    return {'lines': entries[:limit], 'service': service, 'allowed_services': list(SERVICES),
            'limit': limit, 'errors': errors}
