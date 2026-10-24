"""The admin site can't write: GET-only routes, read-only DB, masked secrets."""
import hashlib
import json
import re
import sqlite3
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from admin.db import open_ro
from admin.web import create_app
from arena.tests.test_auth import login, server  # noqa: F401  (server is a fixture)

ROOT = Path(__file__).resolve().parents[2]
KEY = "sk-" + "a1B2c3D4" * 5
SAMPLE_PATHS = ("/admin", "/admin/", "/admin/api/status", "/admin/api/logs",
                "/admin/api/tables", "/admin/api/tables/items",
                "/admin/api/tables/items/rows/1", "/admin/api/tables/items/export.csv",
                "/admin/static/admin.js")


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "league.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT, api_key TEXT, "
                     "token TEXT, password TEXT, secret_x TEXT, created_date TEXT)")
        conn.execute("INSERT INTO items VALUES (1, 'plain', ?, ?, ?, ?, '2026-01-01')",
                     (KEY, KEY, KEY, KEY))
    conn.close()
    monkeypatch.setenv("ARENA_DB_PATH", str(path))
    return path


def admin_routes():
    return [r for r in create_app().routes if hasattr(r, "path")]


def test_every_admin_route_is_get_only():
    routes = admin_routes()
    assert routes
    for r in routes:
        assert r.path.startswith("/admin"), r.path
        methods = getattr(r, "methods", None)
        if methods is not None:  # static mount has no method list
            assert set(methods) <= {"GET", "HEAD"}, (r.path, methods)
        params = [p.name for p in getattr(r, "dependant").query_params] if hasattr(r, "dependant") else []
        assert not {"sql", "query", "statement"} & set(params), r.path
        assert "sql" not in r.path.lower()


def test_caddy_forward_auth_covers_every_admin_path():
    text = (ROOT / "Caddyfile").read_text()
    at = text.index("@admin path /admin /admin/*")
    block = text[at:text.index("reverse_proxy admin:8200", at)]
    assert "forward_auth api:8000" in block and "uri /auth/check" in block
    patterns = re.search(r"@admin path (.+)", text).group(1).split()
    for r in admin_routes():
        assert any(r.path == p or (p.endswith("/*") and r.path.startswith(p[:-1]))
                   for p in patterns), r.path


@pytest.mark.parametrize("path", SAMPLE_PATHS)
def test_admin_paths_need_login(server, path):  # noqa: F811
    r = server.get("/auth/check", headers={"X-Forwarded-Uri": path}, follow_redirects=False)
    if path.startswith("/admin/api/"):
        assert r.status_code == 401
    else:
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
    login(server)
    assert server.get("/auth/check", headers={"X-Forwarded-Uri": path}).status_code == 204


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_endpoints_never_change_the_database(db):
    before = digest(db)
    with TestClient(create_app()) as c:
        for url in ("/admin/api/tables", "/admin/api/tables/items", "/admin/api/tables/items?q=plain",
                    "/admin/api/tables/items/rows/1", "/admin/api/tables/items/export.csv",
                    "/admin/api/shortcuts"):
            assert c.get(url).status_code == 200, url
        for method in ("post", "put", "patch", "delete"):
            assert getattr(c, method)("/admin/api/tables/items").status_code == 405
    assert digest(db) == before
    assert not list(db.parent.glob("league.db-*"))  # no journal/wal left behind


@pytest.mark.parametrize("sql", ["INSERT INTO items (id) VALUES (9)", "CREATE TABLE t (a)",
                                 "UPDATE items SET name='x'", "DELETE FROM items", "DROP TABLE items"])
def test_open_ro_refuses_writes(db, sql):
    with open_ro() as conn, pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute(sql)


def test_admin_code_has_no_write_or_shell_calls():
    banned = ("INSERT", "UPDATE ", "DELETE", "DROP", "executescript", "subprocess", "docker.sock")
    for f in (ROOT / "admin").glob("*.py"):
        text = f.read_text()
        for word in banned:
            assert word not in text, f"{word!r} found in {f.name}"


def test_secret_columns_are_masked_everywhere(db):
    with TestClient(create_app()) as c:
        row = c.get("/admin/api/tables/items").json()["rows"][0]
        rec = c.get("/admin/api/tables/items/rows/1").json()
        csv_text = c.get("/admin/api/tables/items/export.csv").text
    for col in ("api_key", "token", "password", "secret_x"):
        assert row[col] == "••••" and rec[col] == "••••"
    assert row["name"] == "plain"
    assert KEY not in json.dumps(row) + json.dumps(rec) + csv_text
    assert "••••" in csv_text


def test_admin_container_has_no_secrets_or_write_access():
    svc = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["admin"]
    assert "env_file" not in svc and "environment" not in svc
    assert "docker.sock" not in str(svc)
    assert svc["volumes"] and all(v.endswith(":ro") for v in svc["volumes"])
    docker = (ROOT / "Dockerfile.admin").read_text()
    assert "docker.sock" not in docker and "env_file" not in docker
    assert not re.search(r"(?m)^(COPY|ADD)\s+.*(\.env|data)", docker)
    assert [ln.split()[1:] for ln in docker.splitlines() if ln.startswith("COPY")] == [["admin", "./admin"]]
