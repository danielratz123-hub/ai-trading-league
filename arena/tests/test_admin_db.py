"""Database browser safety and behavior, using only temporary SQLite files."""
import ast
import csv
import io
import json
from pathlib import Path
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from admin.db import open_ro
from admin.masking import is_secret_column, mask_value
from admin.web import create_app
from arena.tests.test_auth import login, server  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def browser(tmp_path, monkeypatch):
    path = tmp_path / 'league.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE items (id INTEGER PRIMARY KEY, title TEXT, api_key TEXT, payload TEXT, created_date TEXT)')
        conn.executemany('INSERT INTO items VALUES (?, ?, ?, ?, ?)', [
            (i, f'item {i:03}', 'private', json.dumps({'nested': [{'token': 'private', 'ok': 7}]}),
             f'2026-{i:03}') for i in range(1, 106)])
        conn.execute('INSERT INTO items VALUES (106, ?, ?, ?, ?)',
                     ('=SUM(1,2)', '', '{"ok": true}', '2026-106'))
        conn.execute('CREATE TABLE "odd"" table" (value TEXT)')
        conn.execute('CREATE TABLE composite (a TEXT, b TEXT, PRIMARY KEY(a,b)) WITHOUT ROWID')
    monkeypatch.setenv('ARENA_DB_PATH', str(path))
    with TestClient(create_app()) as client:
        yield client, path


def test_tables_pagination_sort_search_record(browser):
    client, _ = browser
    tables = client.get('/admin/api/tables').json()
    assert {'name': 'items', 'count': 106} in tables
    assert not any(t['name'].startswith('sqlite_') for t in tables)
    first = client.get('/admin/api/tables/items').json()
    assert first['total'] == 106 and first['page'] == 1 and first['pages'] == 3
    assert len(first['rows']) == 50 and first['rows'][0]['id'] == 106
    assert len(client.get('/admin/api/tables/items?page=3').json()['rows']) == 6
    sorted_rows = client.get('/admin/api/tables/items?sort=title&dir=asc').json()['rows']
    assert sorted_rows[0]['title'] == '=SUM(1,2)'
    found = client.get('/admin/api/tables/items', params={'q': 'item 004'}).json()
    assert found['total'] == 1 and found['rows'][0]['id'] == 4
    assert found['rows'][0]['api_key'] == '••••'
    assert json.loads(found['rows'][0]['payload'])['nested'][0]['token'] == '••••'
    record = client.get('/admin/api/tables/items/rows/4').json()
    assert record['api_key'] == '••••' and record['payload']['nested'][0] == {'token': '••••', 'ok': 7}
    assert client.get('/admin/api/tables/items/rows/9999').status_code == 404
    assert client.get('/admin/api/tables/composite').status_code == 200
    assert client.get('/admin/api/tables/odd%22%20table').status_code == 200


@pytest.mark.parametrize('params,status', [({'sort': 'id; DROP TABLE items'}, 400),
    ({'sort': 'missing'}, 400), ({'dir': 'desc; DELETE FROM items'}, 400),
    ({'page': 0}, 422), ({'per_page': 51}, 422)])
def test_bad_parameters(browser, params, status):
    assert browser[0].get('/admin/api/tables/items', params=params).status_code == status


def test_bad_tables_and_search(browser):
    client, _ = browser
    for suffix in ('', '/rows/1', '/export.csv'):
        assert client.get('/admin/api/tables/items%22%3B--' + suffix).status_code == 404
    assert client.get('/admin/api/tables/items', params={'q': "' OR 1=1 --"}).json()['total'] == 0
    assert client.get('/admin/api/tables/items/export.csv', params={'sort': 'bad'}).status_code == 400


def test_csv_masks_and_protects_formulas(browser):
    client, _ = browser
    response = client.get('/admin/api/tables/items/export.csv')
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 106
    assert rows[0]['title'] == "'=SUM(1,2)"
    assert rows[1]['api_key'] == '••••'
    assert 'private' not in response.text
    assert json.loads(rows[1]['payload'])['nested'][0]['token'] == '••••'
    filtered = client.get('/admin/api/tables/items/export.csv', params={'q': 'item 004'})
    assert len(list(csv.DictReader(io.StringIO(filtered.text)))) == 1


def test_read_only_and_unchanged(browser):
    _, path = browser
    before = path.read_bytes()
    with open_ro() as conn:
        assert conn.execute('PRAGMA query_only').fetchone()[0] == 1
        for sql in ('INSERT INTO items (id) VALUES (999)', 'DELETE FROM items', 'DROP TABLE items'):
            with pytest.raises(sqlite3.OperationalError):
                conn.execute(sql)
        # URI mode remains read-only even if the connection's query-only flag is cleared.
        conn.execute('PRAGMA query_only=OFF')
        with pytest.raises(sqlite3.OperationalError):
            conn.execute('DELETE FROM items')
    assert path.read_bytes() == before


def test_missing_database_is_not_created(tmp_path, monkeypatch):
    path = tmp_path / 'missing.db'
    monkeypatch.setenv('ARENA_DB_PATH', str(path))
    with pytest.raises(sqlite3.OperationalError), open_ro():
        pass
    assert not path.exists()


def test_secret_names():
    for name in ('API_KEY', 'secret', 'Token', 'password', 'passwd', 'hash', 'credential', 'auth'):
        assert is_secret_column(name)
    assert not is_secret_column('title')
    assert mask_value(None) is None and mask_value('') == ''
    assert mask_value(0) == '••••'


def test_routes_are_get_only_and_no_write_sql():
    for route in create_app().routes:
        if hasattr(route, 'methods'):
            assert route.methods == {'GET'}
    connect_calls = []
    for path in (ROOT / 'admin').glob('*.py'):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == 'connect':
                    connect_calls.append((path.name, node.lineno))
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert not re.search(r'\b(INSERT|UPDATE|DELETE|DROP|CREATE|executescript)\b', node.value, re.I)
    assert len(connect_calls) == 1 and connect_calls[0][0] == 'db.py'


def test_new_routes_require_proxy_login(server):  # noqa: F811
    paths = ['/admin', '/admin/', '/admin/static/admin.js', '/admin/api/tables',
             '/admin/api/tables/trades', '/admin/api/tables/trades/rows/1',
             '/admin/api/tables/trades/export.csv', '/admin/api/shortcuts']
    for path in paths:
        response = server.get('/auth/check', headers={'X-Forwarded-Uri': path}, follow_redirects=False)
        assert response.status_code in (401, 303)
    login(server)
    for path in paths:
        assert server.get('/auth/check', headers={'X-Forwarded-Uri': path}).status_code == 204
    caddy = (ROOT / 'Caddyfile').read_text()
    assert '@admin path /admin /admin/*' in caddy
    block = caddy.split('handle @admin {', 1)[1].split('reverse_proxy admin:8200', 1)[0]
    assert 'forward_auth api:8000' in block and 'uri /auth/check' in block


def test_csv_row_cap(browser):
    client, path = browser
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE many (value TEXT)')
        conn.executemany('INSERT INTO many VALUES (?)', [('plain',)] * 50002)
    response = client.get('/admin/api/tables/many/export.csv')
    assert len(list(csv.reader(io.StringIO(response.text)))) == 50001


def test_shortcuts_reference_league_schema(browser):
    from arena.db import SCHEMA

    shortcuts = browser[0].get('/admin/api/shortcuts').json()
    assert set(shortcuts) == {'trades', 'positions', 'strategies', 'agent_reports', 'weekly_scores'}
    for table in shortcuts.values():
        assert f'CREATE TABLE IF NOT EXISTS {table} (' in SCHEMA
