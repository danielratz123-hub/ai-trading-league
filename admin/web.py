"""The page at /admin and its small read-only JSON API.

Login: Caddy asks the league dashboard (/auth/check) before any /admin request
reaches this app, so only Joe's signed-in browser gets here. This app only
has GET routes and its data mounts are read-only.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Config

STATIC = Path(__file__).parent / "static"
NO_STORE = {"Cache-Control": "no-store"}


def create_app(cfg: Config | None = None) -> FastAPI:
    app = FastAPI(title="Admin", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/admin/healthz")
    def healthz():
        return JSONResponse({"ok": True}, headers=NO_STORE)

    @app.get("/admin/api/ping")
    def ping():
        return JSONResponse({"ok": True, "app": "admin"}, headers=NO_STORE)

    @app.get("/admin/")
    @app.get("/admin")
    def shell():
        return FileResponse(STATIC / "index.html", headers=NO_STORE)

    app.mount("/admin/static", StaticFiles(directory=STATIC), name="static")
    return app
