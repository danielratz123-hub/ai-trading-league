"""Read-only operational status. No league imports: that code isn't in this image."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import os
import re
from urllib.request import Request
from zoneinfo import ZoneInfo

from . import db
from .config import load

ET = ZoneInfo('America/New_York')
UTC = timezone.utc
# Copied from arena/health.py; importing league modules isn't possible in admin.
HEARTBEAT_STALE = 180
# arena/scheduler.py and config/learning.yaml defaults. No env/key loading here.
EXECUTE_AT = '09:50'
DECIDE_AT = '10:00'
DAILY_AFTER_CLOSE = timedelta(minutes=45)
JOBS = ('execute', 'daily', 'decide', 'risk-check', 'daily-review', 'weekly-review')
LEARNING = JOBS[3:]


def timestamp(value):
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def at(day, hm):
    return datetime.fromisoformat(f'{day}T{hm}').replace(tzinfo=ET)


def guarded(collector):
    try:
        return collector()
    except Exception as exc:
        # Exception messages can contain URLs, credentials, SQL or event payloads.
        return {'error': type(exc).__name__}


def fetch(url, timeout=3):
    # No redirects: only the configured container health endpoint is contacted.
    from urllib.request import HTTPRedirectHandler, build_opener

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    with build_opener(NoRedirect).open(Request(url, method='GET'), timeout=timeout) as response:
        return response.status


def service(url, get):
    try:
        return {'status': 'up' if get(url, timeout=3) == 200 else 'down'}
    except Exception as exc:
        return {'status': 'down', 'error': type(exc).__name__}


def version(path):
    try:
        head = (path / 'HEAD').read_text().strip()
        branch = None
        if head.startswith('ref: '):
            ref = head[5:]
            if not ref.startswith('refs/heads/') or '..' in ref.split('/'):
                return None
            branch = ref[len('refs/heads/'):]
            target = (path / ref).resolve()
            if not target.is_relative_to(path.resolve()):
                return None
            if target.is_file():
                head = target.read_text().strip()
            else:
                head = next(line.split()[0] for line in (path / 'packed-refs').read_text().splitlines()
                            if len(line.split()) == 2 and line.split()[1] == ref)
        if not re.fullmatch(r'[0-9a-fA-F]{40,64}', head):
            return None
        return {'sha': head[:7], 'branch': branch}
    except Exception:
        return None


def heartbeat(cfg, now):
    beat = float((cfg.data_dir / 'scheduler.heartbeat').read_text())
    if not math.isfinite(beat) or beat > now.timestamp() + 30:
        raise ValueError('Invalid heartbeat')
    age = max(0, now.timestamp() - beat)
    # The file is overwritten, so it cannot prove the process start time.
    # A recorded start is preferred; otherwise report a lower bound from this beat.
    start = datetime.fromtimestamp(beat, UTC)
    source = 'heartbeat_lower_bound'
    with db.open_ro() as conn:
        row = conn.execute("SELECT ts FROM events_log WHERE source IN ('scheduler', 'scheduler-start') "
                           "AND level = 'info' AND lower(message) LIKE '%start%' "
                           "ORDER BY ts DESC, id DESC LIMIT 1").fetchone()
    if row and timestamp(row['ts']) <= now:
        start, source = timestamp(row['ts']), 'events_log'
    return {'heartbeat_age_s': round(age, 1), 'heartbeat_stale': age > HEARTBEAT_STALE,
            'last_restart': start.isoformat() if source == 'events_log' else None,
            'uptime_s': max(0, int((now - start).total_seconds())), 'uptime_source': source}


def scheduler(now):
    with db.open_ro() as conn:
        calendar = [dict(r) for r in conn.execute('SELECT * FROM calendar ORDER BY date')]
        rows = [dict(r) for r in conn.execute(
            'SELECT job, key, started_at, finished_at, exit_code FROM job_runs ORDER BY started_at, id')]
        started = conn.execute('SELECT 1 FROM agents LIMIT 1').fetchone() is not None
    if not calendar or calendar[-1]['date'] <= now.astimezone(ET).date().isoformat():
        raise ValueError('Calendar coverage missing')
    learning_observed = any(r['job'] in LEARNING for r in rows)
    # The admin has no engine environment. Monitor the requested learning jobs
    # using documented defaults even before their first recorded run.
    learning = True
    slots = {job: [] for job in JOBS}
    for index, day in enumerate(calendar):
        d = day['date']
        slots['execute'].append((at(d, EXECUTE_AT), d))
        daily = at(d, day['close_time']) + DAILY_AFTER_CLOSE
        slots['daily'].append((daily, d))
        # Daily review waits for daily, even though configured for 16:30.
        slots['daily-review'].append((max(at(d, '16:30'), daily), f'daily-review-{d}'))
        limit = min(15 * 60 + 30, int(day['close_time'][:2]) * 60 + int(day['close_time'][3:]) - 30)
        minutes = sorted(set(list(range(600, limit + 1, 60)) + ([limit] if limit >= 600 else [])))
        for minute in minutes:
            hm = f'{minute // 60:02}:{minute % 60:02}'
            slots['risk-check'].append((at(d, hm), f'risk-{d}-{hm.replace(":", "")}'))
        # A next calendar row is required to prove the week has ended (holidays included).
        if index + 1 < len(calendar) and timestamp(d).isocalendar()[:2] != timestamp(calendar[index + 1]['date']).isocalendar()[:2]:
            tomorrow = (at(d, '00:00') + timedelta(days=1)).date()
            review_day = tomorrow + timedelta(days=(5 - tomorrow.weekday()) % 7)
            review = at(review_day.isoformat(), '12:00')
            decide = at(tomorrow.isoformat(), DECIDE_AT)
            if learning:
                attempts = [r for r in rows if r['job'] == 'weekly-review' and r['key'] == f'weekly-review-{d}']
                complete = next((timestamp(r['finished_at']) for r in attempts
                                 if r['exit_code'] == 0 and r['finished_at']), None)
                decide = max(decide, complete or review + timedelta(hours=4))
            slots['decide'].append((decide, d))
            slots['weekly-review'].append((review, f'weekly-review-{d}'))
    result = {}
    for job in JOBS:
        history = [r for r in rows if r['job'] == job]
        latest = history[-1] if history else None
        enabled = started and (job not in LEARNING or learning)
        due = [(t, key) for t, key in slots[job] if t < now] if enabled else []
        expected = due[-1] if due else None
        exists = expected and any(r['key'] == expected[1] for r in history)
        future = [t for t, _ in slots[job] if t >= now] if enabled else []
        result[job] = {'started_at': latest['started_at'] if latest else None,
                       'finished_at': latest['finished_at'] if latest else None,
                       'exit_code': latest['exit_code'] if latest else None,
                       'result': ('running' if latest['exit_code'] is None else
                                  'ok' if latest['exit_code'] == 0 else 'failed') if latest else None,
                       'enabled': enabled, 'next_expected_run': min(future).isoformat() if future else None,
                       'expected_at': expected[0].isoformat() if expected else None,
                       'late': bool(expected and not exists)}
    return {'jobs': result, 'learning_observed': learning_observed,
            'schedule_basis': 'documented_defaults'}


def agents():
    result = {}
    with db.open_ro() as conn:
        for agent in ('claude', 'gpt'):
            row = conn.execute('SELECT started_at, finished_at, status FROM agent_turns '
                               'WHERE agent_id = ? ORDER BY started_at DESC, id DESC LIMIT 1', (agent,)).fetchone()
            duration = (timestamp(row['finished_at']) - timestamp(row['started_at'])).total_seconds() if row and row['finished_at'] else None
            result[agent] = {'time': row['started_at'] if row else None,
                             'result': ('ok' if row['status'] == 'accepted' else 'failed') if row else None,
                             'duration_s': duration}
    return result


def broker(now):
    with db.open_ro() as conn:
        day = conn.execute('SELECT * FROM calendar WHERE date = ?',
                           (now.astimezone(ET).date().isoformat(),)).fetchone()
        events = conn.execute("SELECT ts, level FROM events_log WHERE lower(source) IN "
                              "('alpaca', 'prices', 'mirror', 'reconcile') "
                              "OR lower(source) LIKE 'alpaca%' ORDER BY ts DESC LIMIT 1000").fetchall()
        # Date-only bars/order logs are evidence for that market day, not a live probe.
        dates = [conn.execute(f'SELECT MAX(date) FROM {table}').fetchone()[0]
                 for table in ('prices', 'mirror_log')]
    successes = [timestamp(r['ts']) for r in events if r['level'].lower() == 'info']
    successes += [at(d, '00:00') for d in dates if d]
    last = max((t for t in successes if t <= now), default=None)
    # Four days spans weekends and common US market holiday weekends.
    recent = now - timedelta(days=4)
    errors = any(recent <= timestamp(r['ts']) <= now and r['level'].lower() in ('error', 'critical') for r in events)
    market_open = bool(day and at(day['date'], day['open_time']) <= now < at(day['date'], day['close_time']))
    return {'status': 'failed' if errors else 'ok' if last and last >= recent else 'unknown',
            'last_success': last.isoformat() if last else None,
            'market': 'open' if market_open else 'closed', 'evidence': 'database_only'}


def collect(cfg=None, *, get=fetch, now=None):
    cfg = cfg or load()
    now = now or datetime.now(UTC)
    league = service(os.getenv('ADMIN_LEAGUE_URL', 'http://api:8000/healthz'), get)
    beat = guarded(lambda: heartbeat(cfg, now))
    league.update(beat)
    result = {'league_app': league,
              'agent_hub': service(os.getenv('ADMIN_HUB_URL', 'http://hub:8100/hub/healthz'), get),
              'scheduler': guarded(lambda: scheduler(now)), 'agents': guarded(agents),
              'broker': guarded(lambda: broker(now)), 'version': version(cfg.repo_git)}
    red, yellow = [], []
    if league['status'] == 'down':
        red.append('League app is down')
    if beat.get('error') or beat.get('heartbeat_stale'):
        red.append('Scheduler heartbeat is stale or unavailable')
    if result['agent_hub']['status'] == 'down':
        yellow.append('Agent hub is down')
    for name in ('scheduler', 'agents', 'broker'):
        if 'error' in result[name]:
            yellow.append(f'{name} status is unavailable')
    for job, status in result['scheduler'].get('jobs', {}).items():
        if status['late'] or status['result'] == 'failed':
            yellow.append(f'{job} is late or failed')
    for agent, status in result['agents'].items():
        if isinstance(status, dict) and status.get('result') == 'failed':
            yellow.append(f'{agent} last run failed')
    if result['broker'].get('status') in ('failed', 'unknown'):
        yellow.append('Broker evidence is failed or stale')
    result['overall'] = {'colour': 'red' if red else 'yellow' if yellow else 'green',
                         'reasons': red + yellow}
    return result
