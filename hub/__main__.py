"""python -m hub  ->  the page on :8100 plus the worker thread."""
from __future__ import annotations

import logging
import os
import threading

import uvicorn

from . import config
from .db import DB
from .github import GitHub
from .notify import Telegram
from .web import create_app
from .worker import Worker


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = config.load()
    db = DB(cfg.db_path)
    worker = None
    if cfg.configured:
        worker = Worker(cfg, db, GitHub(cfg.repo, cfg.token),
                        notify=Telegram(cfg.telegram_token, cfg.telegram_chat))
        threading.Thread(target=worker.forever, daemon=True, name="hub-worker").start()
    else:
        logging.getLogger("hub").warning("HUB_REPO / HUB_GITHUB_TOKEN missing: page only, no worker")
    uvicorn.run(create_app(cfg, db, worker), host=os.getenv("HUB_HOST", "0.0.0.0"),
                port=int(os.getenv("HUB_PORT", "8100")), log_level="warning", proxy_headers=False)


if __name__ == "__main__":
    main()
