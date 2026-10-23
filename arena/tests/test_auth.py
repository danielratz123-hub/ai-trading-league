"""The login in front of the dashboard and API (arena/api/auth.py)."""
import pytest
from fastapi.testclient import TestClient

from arena.api import auth
from arena.db import connect

PASSWORD = "correct horse battery staple"
TOKEN = "t" * 40


@pytest.fixture
def server(tmp_path, monkeypatch):
    """The app as it runs on the VPS (ARENA_SERVER=1)."""
    db = tmp_path / "arena.db"
    connect(db).close()
    monkeypatch.setenv("ARENA_DB", str(db))
    monkeypatch.setenv("ARENA_SERVER", "1")
    monkeypatch.setenv("ARENA_DISABLE_CONTROL", "1")
    monkeypatch.setenv("ADMIN_TOKEN", TOKEN)
    # As written in .env for Docker Compose ($ doubled).
    monkeypatch.setenv("DASHBOARD_PASSWORD_HASH", auth.hash_password(PASSWORD).replace("$", "$$"))
    monkeypatch.setattr(auth, "LIMITER", auth.RateLimiter())
    import arena.api.app as app_module
    # https: the session cookie is Secure, so the test client must use HTTPS.
    with TestClient(app_module.create_app(), base_url="https://testserver") as c:
        yield c


def login(c, password=PASSWORD, ip="203.0.113.7", follow=False):
    return c.post("/login", data={"password": password, "next": "/"},
                  headers={"X-Forwarded-For": ip}, follow_redirects=follow)


def test_password_hash_roundtrip_and_no_plaintext():
    h = auth.hash_password(PASSWORD)
    assert h.startswith("scrypt$") and PASSWORD not in h
    assert auth.verify_password(PASSWORD, h)
    assert not auth.verify_password("wrong", h)
    assert not auth.verify_password(PASSWORD, "garbage")
    assert auth.hash_password(PASSWORD) != h                # salted


def test_everything_needs_a_session_except_login_and_healthz(server):
    c = server
    assert c.get("/healthz").status_code == 200
    assert c.get("/api/status").status_code == 401
    assert c.get("/api/docs").status_code in (303, 401)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    page = c.get("/login")
    assert page.status_code == 200 and 'name="robots" content="noindex' in page.text
    assert page.headers["X-Robots-Tag"] == "noindex, nofollow"


def test_login_sets_a_secure_cookie_and_unlocks_the_api(server):
    c = server
    assert login(c, "nope").status_code == 401
    r = login(c)
    assert r.status_code == 303
    cookie = r.headers["set-cookie"].lower()
    for flag in ("httponly", "secure", "samesite=strict", "path=/"):
        assert flag in cookie
    assert c.get("/api/status").status_code == 200
    c.post("/logout")
    assert c.get("/api/status").status_code == 401


def test_forged_or_expired_sessions_are_refused(server):
    c = server
    exp, sig = auth.make_session().split(".")
    c.cookies.set(auth.COOKIE, f"{int(exp) + 999999}.{sig}")           # extended expiry
    assert c.get("/api/status").status_code == 401
    c.cookies.set(auth.COOKIE, auth.make_session(now=0))                # long expired
    assert c.get("/api/status").status_code == 401
    c.cookies.set(auth.COOKIE, auth.make_session())
    assert c.get("/api/status").status_code == 200


def test_changing_the_password_logs_everyone_out(server, monkeypatch):
    c = server
    login(c)
    assert c.get("/api/status").status_code == 200
    monkeypatch.setenv("DASHBOARD_PASSWORD_HASH", auth.hash_password("a different password"))
    assert c.get("/api/status").status_code == 401


def test_login_is_rate_limited_per_address(server):
    c = server
    for _ in range(auth.PER_CLIENT):
        assert login(c, "wrong", ip="198.51.100.1").status_code == 401
    # Even the right password is refused while blocked...
    assert login(c, PASSWORD, ip="198.51.100.1").status_code == 429
    # ...but another address is unaffected.
    assert login(c, PASSWORD, ip="198.51.100.2").status_code == 303


def test_global_limit_stops_many_addresses():
    lim = auth.RateLimiter(per_client=5, total=10, window=900)
    for i in range(10):
        lim.failed(f"10.0.0.{i}", now=1000)
    assert lim.blocked("10.9.9.9", now=1001)
    assert not lim.blocked("10.9.9.9", now=1000 + 901)        # window passed


def test_open_redirect_is_not_possible(server):
    c = server
    for bad in ("//evil.example", "https://evil.example/x", "evil"):
        r = c.post("/login", data={"password": PASSWORD, "next": bad}, follow_redirects=False)
        assert r.headers["location"] == "/"


def test_server_refuses_to_start_without_a_strong_admin_token_or_password(monkeypatch):
    monkeypatch.setenv("ARENA_SERVER", "1")
    monkeypatch.setenv("DASHBOARD_PASSWORD_HASH", auth.hash_password(PASSWORD))
    monkeypatch.setenv("ADMIN_TOKEN", "short")
    with pytest.raises(auth.ConfigError, match="ADMIN_TOKEN"):
        auth.check_config()
    monkeypatch.setenv("ADMIN_TOKEN", TOKEN)
    auth.check_config()
    monkeypatch.delenv("DASHBOARD_PASSWORD_HASH")
    with pytest.raises(auth.ConfigError, match="DASHBOARD_PASSWORD_HASH"):
        auth.check_config()
    monkeypatch.setenv("ARENA_SERVER", "0")
    auth.check_config()                                        # on your PC: optional


def test_on_the_pc_login_is_off_unless_a_password_is_set(tmp_path, monkeypatch):
    db = tmp_path / "arena.db"
    connect(db).close()
    monkeypatch.setenv("ARENA_DB", str(db))
    monkeypatch.delenv("ARENA_SERVER", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD_HASH", raising=False)
    import arena.api.app as app_module
    with TestClient(app_module.create_app()) as c:
        assert c.get("/api/status").status_code == 200


def test_auth_check_for_the_agent_hub(server):
    """Caddy asks /auth/check before passing /hub requests to the Agent Hub."""
    r = server.get("/auth/check", headers={"X-Forwarded-Uri": "/hub/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/hub/"
    r = server.get("/auth/check", headers={"X-Forwarded-Uri": "/hub/api/state"}, follow_redirects=False)
    assert r.status_code == 401
    r = server.get("/auth/check", headers={"X-Forwarded-Uri": "//evil.com"}, follow_redirects=False)
    assert r.headers["location"] == "/login?next=/"
    login(server)
    assert server.get("/auth/check", headers={"X-Forwarded-Uri": "/hub/"}).status_code == 204
