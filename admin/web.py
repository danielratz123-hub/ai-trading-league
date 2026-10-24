"""The page at /admin and its small read-only JSON API.

Login: Caddy asks the league dashboard (/auth/check) before any /admin request
reaches this app, so only Joe's signed-in browser gets here. This app only
has GET routes and its data mounts are read-only.
"""
from __future__ import annotations

import csv
import io
import math
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import Config
from . import db, status
from .masking import mask_record

STATIC = Path(__file__).parent / "static"
NO_STORE = {"Cache-Control": "no-store"}


def create_app(cfg: Config | None = None) -> FastAPI:
    app = FastAPI(title="Admin", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/admin/healthz")
    def healthz():
        return JSONResponse({"ok": True}, headers=NO_STORE)

    @app.get("/admin/api/status")
    def operational_status():
        return JSONResponse(status.collect(cfg), headers=NO_STORE)

    @app.get("/admin/api/ping")
    def ping():
        return JSONResponse({"ok": True, "app": "admin"}, headers=NO_STORE)

    @app.get("/admin/")
    @app.get("/admin")
    def shell():
        return FileResponse(STATIC / "index.html", headers=NO_STORE)

    @app.get("/admin/api/tables")
    def tables():
        with db.open_ro() as conn:
            result = [{"name": name, "count": conn.execute(
                f"SELECT COUNT(*) FROM {db.quote(name)}").fetchone()[0]}
                for name in db.tables(conn)]
        return JSONResponse(result, headers=NO_STORE)

    @app.get("/admin/api/shortcuts")
    def shortcuts():
        return JSONResponse({"trades": "trades", "positions": "positions",
                             "strategies": "strategy_code", "agent_reports": "agent_turns",
                             "weekly_scores": "scores"}, headers=NO_STORE)

    @app.get("/admin/api/tables/{name}")
    def table(name: str, page: int = Query(1, ge=1),
              per_page: int = Query(50, ge=50, le=50), sort: str | None = None,
              dir: str = "desc", q: str = ""):
        with db.open_ro() as conn:
            columns, base, ordering, args, _ = db.view(conn, name, sort, dir, q)
            total = conn.execute("SELECT COUNT(*)" + base, args).fetchone()[0]
            rows = conn.execute("SELECT *" + base + ordering + " LIMIT ? OFFSET ?",
                                [*args, per_page, (page - 1) * per_page])
            result = {"columns": columns, "rows": [mask_record(dict(r)) for r in rows],
                      "total": total, "page": page, "pages": math.ceil(total / per_page)}
        return JSONResponse(result, headers=NO_STORE)

    @app.get("/admin/api/tables/{name}/rows/{rowid}")
    def record(name: str, rowid: int):
        with db.open_ro() as conn:
            _, _, _, _, identity = db.view(conn, name, None, "desc", "")
            if identity is None:
                raise HTTPException(404, "Table has no rowid")
            table_name, _ = db.schema(conn, name)
            row = conn.execute(f"SELECT * FROM {db.quote(table_name)} WHERE {identity} = ?",
                               (rowid,)).fetchone()
            if row is None:
                raise HTTPException(404, "Unknown record")
            result = mask_record(dict(row), parse_json=True)
        return JSONResponse(result, headers=NO_STORE)

    @app.get("/admin/api/tables/{name}/export.csv")
    def export(name: str, sort: str | None = None, dir: str = "desc", q: str = ""):
        # Validate before returning a streaming response so bad names produce normal errors.
        with db.open_ro() as conn:
            db.view(conn, name, sort, dir, q)

        def generate():
            def cell(value):
                text = "" if value is None else str(value)
                return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text

            with db.open_ro() as conn:
                columns, base, ordering, args, _ = db.view(conn, name, sort, dir, q)
                buffer = io.StringIO()
                writer = csv.writer(buffer)
                writer.writerow([cell(c) for c in columns])
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate(0)
                for index, row in enumerate(conn.execute(
                        "SELECT *" + base + ordering + " LIMIT 50000", args), 1):
                    masked = mask_record(dict(row))
                    writer.writerow([cell(masked[c]) for c in columns])
                    if index % 500 == 0:
                        yield buffer.getvalue()
                        buffer.seek(0)
                        buffer.truncate(0)
                if buffer.tell():
                    yield buffer.getvalue()

        return StreamingResponse(generate(), media_type="text/csv", headers={
            **NO_STORE, "Content-Disposition": 'attachment; filename="export.csv"'})

    app.mount("/admin/static", StaticFiles(directory=STATIC), name="static")
    return app
