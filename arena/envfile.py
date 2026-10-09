"""Read and update the .env file from the setup page.

Only a fixed list of settings can be changed, values can't contain line
breaks (which could inject other settings), and comments / other lines in
the file are kept as they are. Secret values are never sent back to the
browser: describe() only says whether they're set, plus the last 4
characters so you can recognize which key is saved.
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
from pathlib import Path

from .config import PROJECT_ROOT

ENV_PATH = PROJECT_ROOT / ".env"
EXAMPLE_PATH = PROJECT_ROOT / ".env.example"

SECRET_KEYS = {"ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ADMIN_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "TELEGRAM_BOT_TOKEN",
               "ALPACA_MIRROR_CLAUDE_KEY", "ALPACA_MIRROR_CLAUDE_SECRET",
               "ALPACA_MIRROR_GPT_KEY", "ALPACA_MIRROR_GPT_SECRET"}
PLAIN_KEYS = {"CLAUDE_MODEL", "GPT_MODEL", "REASONING_EFFORT", "MIRROR_PAPER", "MIRROR_MODE", "AUTOSTART_SCHEDULER",
              "TELEGRAM_CHAT_ID"}
EDITABLE = SECRET_KEYS | PLAIN_KEYS
CHOICES = {"REASONING_EFFORT": {"low", "medium", "high"}, "MIRROR_PAPER": {"0", "1"},
           "MIRROR_MODE": {"off", "leader", "each", "combined"},
           "AUTOSTART_SCHEDULER": {"0", "1"}}

_LINE = re.compile(r"^\s*#?\s*([A-Z_][A-Z0-9_]*)\s*=(.*)$")


def ensure_env_file(path: Path | None = None) -> Path:
    """Create .env from the example on first use, with a random admin token."""
    path = path or ENV_PATH
    if not path.exists():
        if EXAMPLE_PATH.exists():
            shutil.copyfile(EXAMPLE_PATH, path)
        else:
            path.write_text("", encoding="utf-8")
    if not read_values(path).get("ADMIN_TOKEN"):
        set_values({"ADMIN_TOKEN": secrets.token_urlsafe(24)}, path)
    return path


def read_values(path: Path | None = None) -> dict[str, str]:
    """Active (uncommented) KEY=value pairs."""
    path = path or ENV_PATH
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _LINE.match(line)
        if m:
            out[m.group(1)] = m.group(2).split(" #")[0].strip()
    return out


def validate(updates: dict[str, str]) -> dict[str, str]:
    clean = {}
    for key, value in updates.items():
        if key not in EDITABLE:
            raise ValueError(f"{key} can't be changed here.")
        value = str(value).strip()
        if any(c in value for c in "\r\n\0"):
            raise ValueError(f"{key} must be a single line.")
        if key in CHOICES and value not in CHOICES[key]:
            raise ValueError(f"{key} must be one of {sorted(CHOICES[key])}.")
        if len(value) > 500:
            raise ValueError(f"{key} is too long.")
        clean[key] = value
    return clean


def set_values(updates: dict[str, str], path: Path | None = None) -> None:
    """Write KEY=value lines (replacing an existing or commented-out line for
    the same key, else appending), and update this process's environment so
    commands started from now on see the new values."""
    path = path or ENV_PATH
    updates = validate(updates)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    done = set()
    for i, line in enumerate(lines):
        m = _LINE.match(line)
        if m and m.group(1) in updates and m.group(1) not in done:
            lines[i] = f"{m.group(1)}={updates[m.group(1)]}"
            done.add(m.group(1))
    lines += [f"{k}={v}" for k, v in updates.items() if k not in done]
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        tmp.replace(path)                 # atomic: never a half-written .env
    except OSError:
        # On the server .env is a single file mounted into the container,
        # which can't be replaced by a rename; write it in place instead.
        path.write_text(tmp.read_text(encoding="utf-8"), encoding="utf-8")
        tmp.unlink(missing_ok=True)
    for k, v in updates.items():
        os.environ[k] = v


def describe(path: Path | None = None) -> dict[str, dict]:
    """Safe-to-show summary of the editable settings."""
    path = path or ENV_PATH
    values = read_values(path)
    out = {}
    for key in sorted(EDITABLE):
        v = values.get(key, "")
        if key in SECRET_KEYS:
            out[key] = {"set": bool(v), "hint": f"…{v[-4:]}" if len(v) >= 8 else ("set" if v else "")}
        else:
            out[key] = {"set": bool(v), "value": v}
    return out
