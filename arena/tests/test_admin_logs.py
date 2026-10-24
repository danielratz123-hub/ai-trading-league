"""Private log API: bounded local reads, service filters, and credential masking."""
from collections import namedtuple
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from admin import logs, status
from admin.config import Config
from admin.masking import mask_text
from admin.web import create_app


@pytest.fixture
def local(tmp_path, monkeypatch):
    cfg = Config('localhost', 8200, tmp_path, tmp_path / 'git')
    monkeypatch.setenv('ARENA_DB_PATH', str(tmp_path / 'arena.db'))
    with sqlite3.connect(tmp_path / 'arena.db') as conn:
        conn.execute('CREATE TABLE events_log (id INTEGER PRIMARY KEY, ts TEXT, level TEXT, source TEXT, message TEXT)')
        conn.executemany('INSERT INTO events_log VALUES (?, ?, ?, ?, ?)', [
            (1, '2026-10-20 12:01:00', 'warning', 'scheduler', 'job delayed'),
            (2, '2026-10-20T08:02:00-04:00', 'error', 'decide:gpt', 'agent failed'),
            (3, '2026-10-20T12:03:00Z', 'critical', 'mirror', 'broker failed'),
            (4, '2026-10-20T12:04:00Z', 'info', 'league', 'ignore info'),
        ])
    directory = tmp_path / 'logs'
    directory.mkdir()
    return cfg, directory


def write_logs(path, rows):
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))


def row(message, **fields):
    return {'ts': '2026-10-20T12:00:00Z', 'level': 'error', 'msg': message, **fields}


def test_merge_mask_filter_and_limits(local):
    cfg, directory = local
    write_logs(directory / '2026-10-20.jsonl', [
        row('scheduler error', job='daily'),
        row('sk-ABC' + 'x' * 40, agent='gpt', run_id='job-1'),
        row('Bearer abcdef; key=xyz', logger='arena.mirror'),
        row('ignore info', level='info'),
    ])
    with TestClient(create_app(cfg)) as client:
        response = client.get('/admin/api/logs')
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        result = response.json()
        assert len(result['lines']) == 6
        assert result['lines'][0]['message'] == 'broker failed'
        assert result['lines'][1]['message'] == 'agent failed'
        assert result['lines'][2]['message'] == 'job delayed'
        assert 'sk-ABC' not in response.text and 'abcdef' not in response.text and 'xyz' not in response.text
        for service in ('league', 'agents', 'mirror/broker'):
            lines = client.get('/admin/api/logs', params={'service': service}).json()['lines']
            assert len(lines) == 2
            assert all(line['service'] == service for line in lines)
        assert len(client.get('/admin/api/logs?limit=1').json()['lines']) == 1
        assert client.get('/admin/api/logs?limit=0').status_code == 422
        unknown = client.get('/admin/api/logs?service=unexpected')
        assert unknown.status_code == 400
        assert unknown.json()['allowed_services'] == list(logs.SERVICES)
        for method in ('post', 'put', 'patch', 'delete', 'head', 'options'):
            assert getattr(client, method)('/admin/api/logs').status_code == 405


def test_fifty_line_cap_and_db_filter_before_limit(local):
    cfg, directory = local
    base = datetime(2026, 10, 21, tzinfo=timezone.utc)
    write_logs(directory / '2026-10-21.jsonl', [
        row(str(i), ts=(base + timedelta(seconds=i)).isoformat()) for i in range(80)])
    with sqlite3.connect(cfg.data_dir / 'arena.db') as conn:
        conn.executemany('INSERT INTO events_log (ts, level, source, message) VALUES (?, ?, ?, ?)',
                         [('2026-10-22', 'error', 'scheduler', 'more recent') for _ in range(60)])
    with TestClient(create_app(cfg)) as client:
        for suffix in ('', '?limit=500'):
            result = client.get('/admin/api/logs' + suffix).json()
            assert len(result['lines']) == 50 and result['limit'] == 50
        assert len(client.get('/admin/api/logs?service=agents').json()['lines']) == 1
    assert [item['message'] for item in logs.collect(cfg, service='league', limit=2)['lines']] == ['more recent'] * 2


def test_only_two_bounded_file_tails_and_bad_records(local):
    cfg, directory = local
    write_logs(directory / '2026-10-18.jsonl', [row('older file', ts='2030-01-01')])
    write_logs(directory / '2026-10-19.jsonl', [row('previous file')])
    path = directory / '2026-10-20.jsonl'
    # Huge first line must be skipped; the last line models an incomplete append.
    path.write_bytes(b'x' * (logs.TAIL_BYTES * 2) + b'\n' + b'bad json\n[]\n' +
                     json.dumps(row('good tail')).encode() + b'\n' +
                     json.dumps(row('bad timestamp', ts='oops')).encode() + b'\n' +
                     json.dumps(row('unfinished')).encode())
    assert [r['msg'] for r in logs.tail(path)] == ['good tail', 'bad timestamp']
    result = logs.collect(cfg)
    messages = [line['message'] for line in result['lines']]
    assert 'good tail' in messages and 'previous file' in messages
    assert 'older file' not in messages and 'unfinished' not in messages


@pytest.mark.parametrize('text', [
    'key=short', 'API_KEY="two words"', "password='two words'", 'token:short',
    'Authorization: Bearer short', 'sk-ABC123', 'a' * 40,
    '{"api_key":"short"}', 'abc-def-' * 8,
])
def test_free_text_masking(text):
    masked = mask_text(text)
    assert '••••' in masked
    assert not any(word in masked for word in ('short', 'two words', 'sk-ABC123', 'a' * 40, 'abc-def-'))


def test_missing_sources_report_safe_errors(local, monkeypatch, tmp_path):
    cfg, directory = local
    monkeypatch.setenv('ARENA_DB_PATH', str(tmp_path / 'absent.db'))
    directory.rmdir()
    result = logs.collect(cfg)
    assert result['lines'] == []
    assert result['errors'] == [{'origin': 'events_log', 'error': 'OperationalError'}]
    assert not (tmp_path / 'absent.db').exists()


@pytest.mark.parametrize('percent,warn', [(80, False), (81, True), (96, True)])
def test_machine_proc_counters_and_bytes(tmp_path, monkeypatch, percent, warn):
    cfg = Config('localhost', 8200, tmp_path, tmp_path / 'git')
    proc = tmp_path / 'proc'
    proc.mkdir()
    (proc / 'stat').write_text('cpu 100 0 0 100 0 0 0 0 50 0\n')
    (proc / 'meminfo').write_text(f'MemTotal: 1000 kB\nMemAvailable: {1000 - percent * 10} kB\n')
    (tmp_path / 'arena.db').write_bytes(b'fixture')
    usage = namedtuple('usage', 'total used free')
    monkeypatch.setattr(status.shutil, 'disk_usage', lambda path: usage(100, percent, 100 - percent))

    def sample(seconds):
        assert seconds == 0.2
        (proc / 'stat').write_text(f'cpu {100 + percent} 0 0 {200 - percent} 0 0 0 0 90 0\n')

    monkeypatch.setattr(status.time, 'sleep', sample)
    result = status.machine(cfg, proc)
    assert result['cpu']['percent'] == pytest.approx(percent)
    assert result['cpu']['basis'] == 'proc_stat'
    assert result['ram']['used_bytes'] == percent * 10 * 1024
    assert result['ram']['total_bytes'] == 1000 * 1024
    assert result['disk']['used_bytes'] == percent
    assert result['database']['size_bytes'] == 7
    assert result['warn'] is warn
    assert all(result[name]['warn'] is warn for name in ('cpu', 'ram', 'disk'))


def test_cpu_load_fallback_and_missing_metrics(tmp_path, monkeypatch):
    cfg = Config('localhost', 8200, tmp_path, tmp_path / 'git')
    monkeypatch.setattr(status.os, 'getloadavg', lambda: (2, 0, 0))
    monkeypatch.setattr(status.os, 'cpu_count', lambda: 2)
    result = status.machine(cfg, tmp_path / 'missing_proc')
    assert result['cpu'] == {'percent': 100, 'basis': 'load_average', 'warn': True}
    assert result['ram']['error'] == 'FileNotFoundError'
    assert result['database']['error'] == 'FileNotFoundError'
