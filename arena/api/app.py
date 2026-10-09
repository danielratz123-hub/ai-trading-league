"""Read-only dashboard API (FastAPI) + the static dashboard itself.

Run locally:   python -m arena.main serve        -> http://127.0.0.1:8000
Every endpoint is GET and opens the database read-only, except the two
admin actions (pause / resume), which need the ADMIN_TOKEN from .env.
Everything except /login and /healthz needs a signed-in session when a
dashboard password is set, and always on the server (see auth.py).
"""
from __future__ import annotations

import hmac
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Iterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .. import health, league, ledger
from ..config import SETTINGS
from ..db import connect
from . import auth, control, live, queries

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def db_path() -> str:
    return os.getenv("ARENA_DB", str(SETTINGS.db_path))


def read_conn() -> Iterator[sqlite3.Connection]:
    """A read-only connection per request: the API can never change money."""
    path = Path(db_path())
    if not path.exists():
        raise HTTPException(503, "The league database doesn't exist yet. Run kickoff first.")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def require_admin(authorization: str | None = Header(default=None)) -> None:
    token = os.getenv("ADMIN_TOKEN", "")
    if not token:
        raise HTTPException(503, "Admin actions are disabled: set ADMIN_TOKEN in .env.")
    given = (authorization or "").removeprefix("Bearer ").strip()
    # Constant-time comparison so the token can't be guessed by timing.
    if not hmac.compare_digest(given.encode(), token.encode()):
        raise HTTPException(401, "Wrong admin token.")


def _agent(conn, agent_id: str) -> str:
    if agent_id not in ledger.agent_ids(conn):
        raise HTTPException(404, f"Unknown agent {agent_id!r}")
    return agent_id


@asynccontextmanager
async def lifespan(app: FastAPI):
    auth.check_config()            # on the server: refuse to start without a password / admin token
    if auth.server_mode() and os.getenv("ARENA_DISABLE_CONTROL") == "1":
        from datetime import timedelta

        from .. import alerts
        alerts.send("Dashboard (api) started. If you didn't restart it, the server or container restarted.",
                    level="info", key="api-start", every=timedelta(minutes=10))
    control.autostart_scheduler()
    live.LIVE = live.LivePrices(db_path)
    live.LIVE.start()              # live prices for the dashboard, market hours only
    yield
    live.LIVE.stop()
    control.SCHEDULER.stop()       # the scheduler lives as long as this app


def create_app() -> FastAPI:
    app = FastAPI(title="AI Trading League", docs_url="/api/docs", openapi_url="/api/openapi.json",
                  lifespan=lifespan)
    # The dashboard is served by this same app, so no cross-site access is
    # needed. DASHBOARD_ORIGINS can still allow specific sites (not "*").
    origins = [o.strip() for o in os.getenv("DASHBOARD_ORIGINS", "").split(",") if o.strip() and o.strip() != "*"]
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"],
                           allow_headers=["Authorization"])
    app.middleware("http")(auth.require_login)
    app.include_router(auth.router)

    @app.middleware("http")
    async def no_index(request, call_next):
        # Keep the dashboard out of search engines, so an agent's web search
        # can't stumble on its opponent's current positions.
        resp = await call_next(request)
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        return resp

    @app.get("/healthz")
    def healthz():
        """For Docker's healthcheck and uptime monitors. No login needed, so
        it says only ok / not ok; details are in /api/health."""
        result = health.check(db_path())
        if not result["ok"]:
            raise HTTPException(503, "unhealthy")
        return {"ok": True}

    @app.get("/api/health")
    def health_details():
        return health.check(db_path(), scheduler=True)

    @app.get("/api/status")
    def status(conn=Depends(read_conn)):
        return queries.league_status(conn)

    @app.get("/api/scoreboard")
    def scoreboard(conn=Depends(read_conn)):
        return queries.scoreboard(conn)

    @app.get("/api/equity")
    def equity(conn=Depends(read_conn)):
        return queries.equity_curves(conn)

    @app.get("/api/agents/{agent_id}")
    def agent(agent_id: str, conn=Depends(read_conn)):
        return queries.agent_detail(conn, _agent(conn, agent_id))

    @app.get("/api/agents/{agent_id}/slots")
    def slots(agent_id: str, conn=Depends(read_conn)):
        return queries.slot_curves(conn, _agent(conn, agent_id))

    @app.get("/api/trades")
    def trades(agent: str | None = None, reason: str | None = None,
               limit: int = Query(100, ge=1, le=1000), conn=Depends(read_conn)):
        return queries.trades(conn, agent, reason, limit)

    @app.get("/api/weeks")
    def weeks(conn=Depends(read_conn)):
        return queries.weeks(conn)

    @app.get("/api/research")
    def research(conn=Depends(read_conn)):
        return queries.research(conn)

    @app.get("/api/mirror")
    def mirror_overview(conn=Depends(read_conn)):
        return queries.mirror_overview(conn)

    @app.get("/api/live")
    def live_values(conn=Depends(read_conn)):
        state = live.LIVE.state() if live.LIVE else {"enabled": False, "market_open": False}
        return queries.live_snapshot(conn, state)

    @app.get("/api/costs")
    def costs(conn=Depends(read_conn)):
        return queries.costs(conn)

    @app.post("/api/admin/pause", dependencies=[Depends(require_admin)])
    def pause():
        return _set_paused(True)

    @app.post("/api/admin/resume", dependencies=[Depends(require_admin)])
    def resume():
        return _set_paused(False)

    # Setup / control panel: only on this computer (see control.py).
    if control.control_enabled():
        app.include_router(control.router)

    # The dashboard itself (always served here, behind the login; separate hosting is not supported).
    if WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


def _set_paused(paused: bool) -> dict:
    conn = connect(db_path())
    try:
        league.set_paused(conn, paused)
        return {"paused": league.is_paused(conn)}
    finally:
        conn.close()


app = create_app()
