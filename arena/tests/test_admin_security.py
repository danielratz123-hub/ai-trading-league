"""The admin site (/admin) is private: login, no secrets, read-only mounts."""
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from arena.tests.test_auth import login, server  # noqa: F401  (server is a fixture)

ROOT = Path(__file__).resolve().parents[2]


def test_auth_check_guards_admin(server):  # noqa: F811
    r = server.get("/auth/check", headers={"X-Forwarded-Uri": "/admin/api/x"}, follow_redirects=False)
    assert r.status_code == 401
    r = server.get("/auth/check", headers={"X-Forwarded-Uri": "/admin/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/admin/"
    login(server)
    for uri in ("/admin/", "/admin/api/x"):
        assert server.get("/auth/check", headers={"X-Forwarded-Uri": uri}).status_code == 204


def test_caddy_checks_login_for_admin_before_the_catch_all():
    text = (ROOT / "Caddyfile").read_text()
    at = text.index("@admin path /admin /admin/*")
    block = text[at:text.index("reverse_proxy admin:8200", at)]
    assert "forward_auth api:8000" in block and "uri /auth/check" in block
    assert at < text.index("\thandle {\n")


def test_admin_compose_service_is_read_only_and_secret_free():
    svc = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["admin"]
    assert svc["profiles"] == ["admin"]
    assert "env_file" not in svc and "environment" not in svc
    assert svc["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in svc["security_opt"]
    vols = svc["volumes"]
    assert "arena-data:/app/data:ro" in vols and "./.git:/repo/.git:ro" in vols
    assert all(v.endswith(":ro") for v in vols)
    assert not any("docker.sock" in v for v in vols)
    assert "docker.sock" not in str(svc) and svc.get("privileged") is not True
    assert "healthcheck" in svc and set(svc["networks"]) == {"web", "default"}


def test_admin_image_copies_only_admin():
    text = (ROOT / "Dockerfile.admin").read_text()
    copies = [ln.split()[1:] for ln in text.splitlines() if ln.startswith("COPY")]
    assert copies == [["admin", "./admin"]]
    assert "USER admin" in text


def test_admin_app_routes():
    from admin.web import create_app
    c = TestClient(create_app())
    assert c.get("/admin/healthz").json() == {"ok": True}
    assert c.get("/admin/api/ping").status_code == 200
    page = c.get("/admin/")
    assert page.status_code == 200 and "Status" in page.text and "Database" in page.text
    assert c.post("/admin/api/ping").status_code == 405


def test_admin_image_has_no_socket_or_copied_secrets():
    text = (ROOT / "Dockerfile.admin").read_text()
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert "docker.sock" not in code and "env_file" not in code
    assert not any(ln.startswith(("COPY", "ADD")) and (".env" in ln or "data" in ln)
                   for ln in code.splitlines())
