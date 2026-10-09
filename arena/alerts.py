"""Telegram alerts. Best effort by design: an alert can never block, crash
or fail a job.

    TELEGRAM_BOT_TOKEN   from @BotFather
    TELEGRAM_CHAT_ID     your chat with the bot (see DEPLOY.md)

Without them, alerts are only written to the log. The bot token is never
logged: httpx logs request URLs (which contain the token) at INFO level, so
its logger is kept at WARNING, and errors are logged by type only.

Repeated alerts (say, a login that stays broken all weekend) are throttled
per `key`: the first one is sent right away, then at most one per `every`.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import timedelta
from pathlib import Path

log = logging.getLogger("arena.alerts")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

TIMEOUT_SECONDS = 5.0
MAX_CHARS = 3500          # Telegram's limit is 4096
ICONS = {"info": "ℹ️", "good": "✅", "warning": "⚠️", "error": "🚨"}

# Tests replace this to capture what would be sent.
_sender = None


def configured() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))


def _state_file() -> Path:
    from .config import SETTINGS
    return Path(os.getenv("ARENA_DB", str(SETTINGS.db_path))).resolve().parent / "alerts-state.json"


def _throttled(key: str, every: timedelta) -> bool:
    """True if an alert with this key was sent less than `every` ago.
    Records the send otherwise. Any problem with the state file = not throttled."""
    try:
        path = _state_file()
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        now = time.time()
        if now - state.get(key, 0) < every.total_seconds():
            return True
        state[key] = now
        # Forget keys older than a month so the file stays small.
        state = {k: v for k, v in state.items() if now - v < 31 * 86400}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        return False
    return False


def send(text: str, level: str = "warning", key: str | None = None,
         every: timedelta = timedelta(hours=2)) -> bool:
    """Send one alert. Returns True if it was delivered. Never raises."""
    try:
        if key and _throttled(key, every):
            log.info("alert throttled (%s)", key)
            return False
        msg = f"{ICONS.get(level, '')} AI Trading League\n{text}".strip()[:MAX_CHARS]
        (log.error if level == "error" else log.warning if level == "warning" else log.info)(
            "alert: %s", text.replace("\n", " | ")[:500])
        if _sender is not None:
            return bool(_sender(msg))
        if not configured():
            return False
        import httpx
        r = httpx.post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
                       json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": msg,
                             "disable_web_page_preview": True},
                       timeout=TIMEOUT_SECONDS)
        if r.status_code != 200:
            log.warning("Telegram refused the alert (HTTP %s)", r.status_code)
            return False
        return True
    except Exception as e:  # noqa: BLE001 - alerts must never break the caller
        log.warning("could not send alert (%s)", type(e).__name__)
        return False
