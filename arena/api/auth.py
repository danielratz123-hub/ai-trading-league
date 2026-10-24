"""Login in front of the whole dashboard and API.

    DASHBOARD_PASSWORD_HASH   made by `python -m arena.main hash-password`
                              (scrypt; the password itself is never stored)
    ADMIN_TOKEN               also signs the session cookies (see below)

On the server (ARENA_SERVER=1) both are required and the app refuses to
start without them. On your own PC the login is off unless you set a hash.

Sessions are a signed cookie, no server-side store:
    arena_session = <expiry unix time>.<HMAC-SHA256(expiry)>
The signing key is derived from the password hash and ADMIN_TOKEN, so
changing either one logs every browser out. The cookie is HttpOnly (page
scripts can't read it), Secure (HTTPS only; browsers also allow it on
http://localhost for the SSH tunnel) and SameSite=Strict (other sites can't
make your browser send it).

Wrong passwords are rate-limited per client address: 5 tries per 15
minutes, plus 50 per 15 minutes in total, so even many addresses together
can't guess quickly; scrypt also makes every guess slow.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import os
import secrets
import threading
import time
from collections import deque
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

COOKIE = "arena_session"
SESSION_SECONDS = 7 * 86400
ADMIN_TOKEN_MIN = 32
PER_CLIENT, GLOBAL, WINDOW = 5, 50, 15 * 60
# Reachable without a session.
OPEN_PATHS = {"/login", "/logout", "/healthz", "/style.css", "/favicon.ico", "/auth/check"}

# scrypt cost: ~50 ms and 16 MB per check on a small VPS.
_N, _R, _P = 2**14, 8, 1


class ConfigError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Password hashing
# --------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${_N}${_R}${_P}${b64(salt)}${b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, want = stored.split("$")
        if kind != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r),
                            p=int(p), dklen=len(base64.b64decode(want)), maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(dk, base64.b64decode(want))
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def server_mode() -> bool:
    return os.getenv("ARENA_SERVER", "0") == "1"


def password_hash() -> str:
    # In .env, `$` must be written as `$$` for Docker Compose; accept both.
    return os.getenv("DASHBOARD_PASSWORD_HASH", "").strip().replace("$$", "$")


def login_enabled() -> bool:
    return server_mode() or bool(password_hash())


def check_config() -> None:
    """Called at startup. On the server, refuse to run half-protected."""
    if not server_mode():
        return
    problems = []
    token = os.getenv("ADMIN_TOKEN", "")
    if len(token) < ADMIN_TOKEN_MIN:
        problems.append(f"ADMIN_TOKEN must be set and at least {ADMIN_TOKEN_MIN} characters "
                        f"(it is {len(token)}). Make one: python -c \"import secrets; print(secrets.token_urlsafe(32))\"")
    if not password_hash().startswith("scrypt$"):
        problems.append("DASHBOARD_PASSWORD_HASH is missing or invalid. Make one: "
                        "docker compose run --rm api python -m arena.main hash-password")
    if problems:
        raise ConfigError("Refusing to start on the server:\n  - " + "\n  - ".join(problems))


def _key() -> bytes:
    material = f"arena-session|{password_hash()}|{os.getenv('ADMIN_TOKEN', '')}"
    return hashlib.sha256(material.encode()).digest()


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------

def make_session(now: float | None = None) -> str:
    exp = str(int((time.time() if now is None else now) + SESSION_SECONDS))
    sig = hmac.new(_key(), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def valid_session(value: str | None, now: float | None = None) -> bool:
    if not value or "." not in value:
        return False
    exp, sig = value.split(".", 1)
    good = hmac.new(_key(), exp.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        return False
    try:
        return int(exp) > (time.time() if now is None else now)
    except ValueError:
        return False


# --------------------------------------------------------------------------
# Rate limiting (in memory; a restart resets it, which is fine)
# --------------------------------------------------------------------------

class RateLimiter:
    def __init__(self, per_client: int = PER_CLIENT, total: int = GLOBAL, window: int = WINDOW):
        self.per_client, self.total, self.window = per_client, total, window
        self.fails: dict[str, deque] = {}
        self.all: deque = deque()
        self.lock = threading.Lock()

    def _trim(self, q: deque, now: float) -> None:
        while q and q[0] <= now - self.window:
            q.popleft()

    def blocked(self, client: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self.lock:
            q = self.fails.setdefault(client, deque())
            self._trim(q, now)
            self._trim(self.all, now)
            return len(q) >= self.per_client or len(self.all) >= self.total

    def failed(self, client: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self.lock:
            self.fails.setdefault(client, deque()).append(now)
            self.all.append(now)

    def succeeded(self, client: str) -> None:
        with self.lock:
            self.fails.pop(client, None)


LIMITER = RateLimiter()


def client_address(request: Request) -> str:
    """The visitor's address. Behind Caddy, the connection comes from Caddy,
    which puts the real address in X-Forwarded-For (and overwrites whatever
    the visitor sent there). That header is only trusted on the server,
    where nothing but Caddy can reach the app."""
    if server_mode():
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[-1].strip()
    return request.client.host if request.client else "?"


# --------------------------------------------------------------------------
# Middleware + routes
# --------------------------------------------------------------------------

def _safe_next(target: str | None) -> str:
    """Only redirect back to a path on this site (no //evil.com)."""
    if not target or not target.startswith("/") or target.startswith("//") or urlparse(target).netloc:
        return "/"
    return target


async def require_login(request: Request, call_next):
    if not login_enabled() or request.url.path in OPEN_PATHS:
        return await call_next(request)
    if valid_session(request.cookies.get(COOKIE)):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Not signed in."}, status_code=401)
    nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(nxt)}", status_code=303)


router = APIRouter()

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Sign in · AI Trading League</title>
<link rel="stylesheet" href="/style.css">
<style>
  .login {{ max-width: 360px; margin: 12vh auto; padding: 0 16px; }}
  .login form {{ display: grid; gap: 12px; }}
  .login input {{ width: 100%; }}
</style></head>
<body><main class="login">
  <div class="panel">
    <h1>AI Trading League</h1>
    <p class="muted small">Sign in to see the dashboard.</p>
    {message}
    <form method="post" action="/login">
      <input type="hidden" name="next" value="{next}">
      <label class="small muted" for="pw">Password</label>
      <input id="pw" name="password" type="password" autocomplete="current-password" autofocus required>
      <button class="primary" type="submit">Sign in</button>
    </form>
  </div>
</main></body></html>"""


def _page(next_url: str, message: str = "", status: int = 200) -> HTMLResponse:
    msg = f'<p class="error">{html.escape(message)}</p>' if message else ""
    return HTMLResponse(_PAGE.format(message=msg, next=html.escape(next_url, quote=True)), status_code=status)


@router.get("/login")
def login_page(request: Request, next: str = "/"):
    if not login_enabled() or valid_session(request.cookies.get(COOKIE)):
        return RedirectResponse(_safe_next(next), status_code=303)
    return _page(_safe_next(next))


@router.post("/login")
def login(request: Request, password: str = Form(...), next: str = Form("/")):
    if not login_enabled():
        return RedirectResponse("/", status_code=303)
    client = client_address(request)
    if LIMITER.blocked(client):
        return _page(_safe_next(next), "Too many attempts. Wait 15 minutes and try again.", 429)
    if not verify_password(password, password_hash()):
        LIMITER.failed(client)
        return _page(_safe_next(next), "Wrong password.", 401)
    LIMITER.succeeded(client)
    resp = RedirectResponse(_safe_next(next), status_code=303)
    resp.set_cookie(COOKIE, make_session(), max_age=SESSION_SECONDS, httponly=True,
                    secure=True, samesite="strict", path="/")
    return resp


@router.get("/auth/check")
def auth_check(request: Request) -> Response:
    """Caddy asks this before passing a request to the Agent Hub (/hub)
    or the admin site (/admin):
    204 = signed in. Otherwise pages go to the login and API calls get 401.
    Caddy sends the original path in X-Forwarded-Uri."""
    if not login_enabled() or valid_session(request.cookies.get(COOKIE)):
        return Response(status_code=204)
    uri = request.headers.get("x-forwarded-uri", "/hub/")
    if uri.startswith(("/hub/api/", "/admin/api/")):
        return JSONResponse({"detail": "Not signed in."}, status_code=401)
    return RedirectResponse(f"/login?next={quote(_safe_next(uri))}", status_code=303)


@router.post("/logout")
def logout() -> Response:
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE, path="/")
    return resp
