"""Optional Telegram messages (same bot as the league alerts, if configured)."""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger("hub")


class Telegram:
    def __init__(self, token: str, chat: str):
        self.token, self.chat = token, chat

    def __call__(self, text: str) -> None:
        if not (self.token and self.chat):
            return
        try:
            httpx.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                       json={"chat_id": self.chat, "text": f"🧰 Agent Hub\n{text}"[:3900],
                             "disable_web_page_preview": True}, timeout=15)
        except Exception as e:  # noqa: BLE001 - a lost notification must not stop the hub
            log.warning("telegram failed: %s", type(e).__name__)
