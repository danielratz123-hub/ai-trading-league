"""Status collectors run against temporary files, never services or credentials."""
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest
from fastapi.testclient import TestClient

from admin import status
from admin.config import Config
from admin.web import create_app
from arena.db import SCHEMA
from arena.tests.test_auth import login, server  # noqa: F401

NOW = datetime(2026, 10, 20, 13, 0, tzinfo=timezone.utc)  # Tuesday before execute


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path / 'arena.db'
    # Existing health tests must not depend on the worker's CPU/RAM/disk load.
    original_machine = status.machine
    proc = tmp_path / 'proc'
    proc.mkdir()
    (proc / 'stat').write_text('cpu 10 0 0 90 0 0 0 0\n')
    (proc / 'meminfo').write_text('MemTotal: 1000 kB\nMemAvailable: 800 kB\n')
    monkeypatch.setattr(status.time, 'sleep', lambda seconds: (proc / 'stat').write_text(
        'cpu 20 0 0 180 0 0 0 0\n'))
    from collections import namedtuple
    usage = namedtuple('usage', 'total used free')
    monkeypatch.setattr(status.shutil, 'disk_usage', lambda path: usage(1000, 200, 800))
    monkeypatch.setattr(status.os, 'getloadavg', lambda: (0.1, 0.1, 0.1))
    monkeypatch.setattr(status, 'machine', lambda cfg, proc_dir: original_machine(cfg, proc))
    monkeypatch.setenv('ARENA_DB_PATH', str(path))
    cfg = Config('localhost', 8200, tmp_path, tmp_path / 'git')
    cfg.repo_git.mkdir()
    (cfg.repo_git / 'HEAD').write_text('ref: refs/heads/main\n')
    (cfg.repo_git / 'refs/heads').mkdir(parents=True)
    (cfg.repo_git / 'refs/heads/main').write_text('a' * 40)
    (tmp_path / 'scheduler.heartbeat').write_text(str(NOW.timestamp() - 10))
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        for d in ('2026-10-16', '2026-10-19', '2026-10-20', '2026-10-21', '2026-10-23', '2026-10-26'):
            conn.execute('INSERT INTO calendar VALUES (?, ?, ?)', (d, '09:30', '16:00'))
        for agent in ('claude', 'gpt'):
            conn.execute('INSERT INTO agents VALUES (?, ?, ?)', (agent, 'test', NOW.isoformat()))
            conn.execute('INSERT INTO agent_turns (week_no, agent_id, model, status, started_at, finished_at) '
                         'VALUES (1, ?, ?, ?, ?, ?)', (agent, 'test', 'accepted',
                         (NOW - timedelta(minutes=1)).isoformat(), NOW.isoformat()))
        for job, key in (('execute', '2026-10-19'), ('daily', '2026-10-19'), ('decide', '2026-10-16'),
                         ('risk-check', 'risk-2026-10-19-1530'),
                         ('daily-review', 'daily-review-2026-10-19')):
            conn.execute('INSERT INTO job_runs (job, key, started_at, finished_at, exit_code) VALUES (?, ?, ?, ?, 0)',
                         (job, key, (NOW - timedelta(hours=1)).isoformat(), NOW.isoformat()))
        conn.execute("INSERT INTO job_runs (job, key, started_at, finished_at, exit_code) "
                     "VALUES ('weekly-review', 'weekly-review-2026-10-16', ?, ?, 0)",
                     ('2026-10-17T12:00:00-04:00', '2026-10-17T12:40:00-04:00'))
        conn.execute('INSERT INTO events_log (ts, level, source, message) VALUES (?, ?, ?, ?)',
                     (NOW.isoformat(), 'info', 'alpaca', 'private payload never returned'))
    return cfg, path


def fake_get(url, timeout):
    assert timeout == 3
    assert url in ('http://api:8000/healthz', 'http://hub:8100/hub/healthz')
    return 200


def test_green(setup):
    cfg, _ = setup
    result = status.collect(cfg, get=fake_get, now=NOW)
    assert result['overall'] == {'colour': 'green', 'reasons': []}
    assert result['version'] == {'sha': 'aaaaaaa', 'branch': 'main'}
    assert result['league_app']['uptime_source'] == 'heartbeat_lower_bound'
    assert result['agents']['claude']['duration_s'] == 60
    assert result['broker']['market'] == 'closed'
    assert result['scheduler']['jobs']['execute']['next_expected_run'] == '2026-10-20T09:50:00-04:00'
    assert 'private payload' not in str(result)


def test_red_when_league_down(setup):
    result = status.collect(setup[0], get=lambda url, timeout: 503 if 'api:8000' in url else 200, now=NOW)
    assert result['overall']['colour'] == 'red'
    assert 'League app is down' in result['overall']['reasons']


def test_yellow_late_job(setup):
    # Keep heartbeat fresh while the execute deadline passes.
    (setup[0].data_dir / 'scheduler.heartbeat').write_text(str((NOW + timedelta(minutes=51)).timestamp()))
    result = status.collect(setup[0], get=fake_get, now=NOW + timedelta(minutes=51))
    assert result['overall']['colour'] == 'yellow'
    assert result['scheduler']['jobs']['execute']['late']


def test_stale_heartbeat_and_collector_isolation(setup):
    cfg, path = setup
    (cfg.data_dir / 'scheduler.heartbeat').write_text(str(NOW.timestamp() - 181))
    with sqlite3.connect(path) as conn:
        conn.execute('DROP TABLE agent_turns')
    result = status.collect(cfg, get=fake_get, now=NOW)
    assert result['overall']['colour'] == 'red'
    assert result['agents'] == {'error': 'OperationalError'}
    assert result['broker']['status'] == 'ok'


def test_packed_and_detached_git(setup):
    cfg, _ = setup
    (cfg.repo_git / 'refs/heads/main').unlink()
    (cfg.repo_git / 'packed-refs').write_text('# packed refs\n' + 'b' * 40 + ' refs/heads/main\n')
    assert status.version(cfg.repo_git) == {'sha': 'bbbbbbb', 'branch': 'main'}
    (cfg.repo_git / 'HEAD').write_text('c' * 40)
    assert status.version(cfg.repo_git) == {'sha': 'ccccccc', 'branch': None}
    (cfg.repo_git / 'HEAD').write_text('ref: ../../outside')
    assert status.version(cfg.repo_git) is None


def test_get_only_and_no_store(setup, monkeypatch):
    original = status.collect
    monkeypatch.setattr(status, 'collect', lambda cfg: original(cfg, get=fake_get, now=NOW))
    with TestClient(create_app(setup[0])) as client:
        response = client.get('/admin/api/status')
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        assert response.json()['overall']['colour'] == 'green'
        for method in ('post', 'put', 'patch', 'delete', 'head', 'options'):
            assert getattr(client, method)('/admin/api/status').status_code == 405


@pytest.mark.parametrize('uri', ['/admin', '/admin/', '/admin/api/status', '/admin/api/logs',
                                 '/admin/api/logs?service=agents&limit=50'])
def test_status_requires_proxy_login(server, uri):  # noqa: F811
    from pathlib import Path

    headers = {'X-Forwarded-Uri': uri}
    response = server.get('/auth/check', headers=headers, follow_redirects=False)
    assert response.status_code == (401 if '/api/' in uri else 303)
    login(server)
    assert server.get('/auth/check', headers=headers).status_code == 204
    caddy = (Path(__file__).resolve().parents[2] / 'Caddyfile').read_text()
    assert '@admin path /admin /admin/*' in caddy
    block = caddy.split('handle @admin {', 1)[1].split('reverse_proxy admin:8200', 1)[0]
    assert 'forward_auth api:8000' in block and 'uri /auth/check' in block


def test_broker_errors_agent_failure_and_hub_down(setup):
    cfg, path = setup
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO events_log (ts, level, source, message) VALUES (?, 'error', 'prices', 'hidden')", (NOW.isoformat(),))
        conn.execute("UPDATE agent_turns SET status='failed' WHERE agent_id='gpt'")
    result = status.collect(cfg, get=lambda url, timeout: 500 if 'hub:8100' in url else 200, now=NOW)
    assert result['overall']['colour'] == 'yellow'
    assert result['broker']['status'] == 'failed'
    assert 'gpt last run failed' in result['overall']['reasons']
    assert 'Agent hub is down' in result['overall']['reasons']


def test_half_day_learning_schedule_and_running_job(setup):
    _, path = setup
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE calendar SET close_time='13:00' WHERE date='2026-10-20'")
        conn.execute("INSERT INTO job_runs (job, key, started_at) VALUES ('risk-check', 'risk-2026-10-20-1200', ?)",
                     (NOW.isoformat(),))
    noon = datetime(2026, 10, 20, 16, 15, tzinfo=timezone.utc)
    result = status.scheduler(noon)
    risk = result['jobs']['risk-check']
    assert risk['result'] == 'running'
    assert not risk['late']
    assert risk['next_expected_run'] == '2026-10-20T12:30:00-04:00'
    assert result['jobs']['daily']['next_expected_run'] == '2026-10-20T13:45:00-04:00'
    # Learning daily review still uses its configured 16:30 on half days.
    assert result['jobs']['daily-review']['next_expected_run'] == '2026-10-20T16:30:00-04:00'
    missed = status.scheduler(noon + timedelta(minutes=16))
    assert missed['jobs']['risk-check']['late']


def test_weekly_review_gates_decide(setup):
    saturday = datetime(2026, 10, 17, 14, 0, tzinfo=timezone.utc)
    jobs = status.scheduler(saturday)['jobs']
    assert not jobs['decide']['late']
    assert jobs['decide']['next_expected_run'] == '2026-10-17T12:40:00-04:00'


def test_fetch_failures_hide_details_and_urls_are_overridable(setup, monkeypatch):
    monkeypatch.setenv('ADMIN_LEAGUE_URL', 'http://league/healthz')
    monkeypatch.setenv('ADMIN_HUB_URL', 'http://worker/healthz')
    urls = []

    def fail(url, timeout):
        urls.append(url)
        raise RuntimeError('sensitive response text')

    result = status.collect(setup[0], get=fail, now=NOW)
    assert urls == ['http://league/healthz', 'http://worker/healthz']
    assert result['overall']['colour'] == 'red'
    assert 'sensitive' not in str(result)


def test_missing_db_does_not_create_file(setup, monkeypatch, tmp_path):
    missing = tmp_path / 'missing.db'
    monkeypatch.setenv('ARENA_DB_PATH', str(missing))
    result = status.collect(setup[0], get=fake_get, now=NOW)
    assert result['scheduler'] == {'error': 'OperationalError'}
    assert result['agents'] == {'error': 'OperationalError'}
    assert result['broker'] == {'error': 'OperationalError'}
    assert not missing.exists()


@pytest.mark.parametrize('metric,percent,colour', [
    ('cpu', 80, 'green'), ('cpu', 81, 'yellow'), ('ram', 81, 'yellow'),
    ('disk', 81, 'yellow'), ('disk', 95, 'yellow'), ('disk', 96, 'red'),
])
def test_machine_warnings_raise_overall_status(setup, monkeypatch, metric, percent, colour):
    metrics = {name: {'percent': percent if name == metric else 10,
                      'warn': name == metric and percent > 80}
               for name in ('cpu', 'ram', 'disk')}
    metrics['warn'] = percent > 80
    monkeypatch.setattr(status, 'machine', lambda cfg, proc_dir: metrics)
    result = status.collect(setup[0], get=fake_get, now=NOW)
    assert result['machine'] == metrics
    assert result['overall']['colour'] == colour
