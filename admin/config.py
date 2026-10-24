"""Settings for the admin site. No secrets: it only reads."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    data_dir: Path   # the league's data volume, mounted read-only
    repo_git: Path   # the repo's .git folder, mounted read-only


def load() -> Config:
    return Config(
        host=os.getenv("ADMIN_HOST", "0.0.0.0"),
        port=int(os.getenv("ADMIN_PORT", "8200")),
        data_dir=Path(os.getenv("ADMIN_DATA_DIR", "/app/data")),
        repo_git=Path(os.getenv("ADMIN_REPO_GIT", "/repo/.git")),
    )
