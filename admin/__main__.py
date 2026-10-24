"""python -m admin  ->  the admin site on :8200."""
from __future__ import annotations

import uvicorn

from . import config
from .web import create_app


def main() -> None:
    cfg = config.load()
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="warning",
                proxy_headers=False)


if __name__ == "__main__":
    main()
