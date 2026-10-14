"""Minimal Telegram Bot API client (stdlib only).

Long-polling receiver + send/edit helpers. No third-party deps so the
server install stays lean. All network calls have timeouts and raise
BotError on Telegram-level failures.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request


class BotError(Exception):
    pass


class Bot:
    def __init__(self, token: str, timeout: int = 60):
        if not token or ":" not in token:
            raise ValueError("bad bot token")
        self._base = f"https://api.telegram.org/bot{token}"
        self._timeout = timeout

    def call(self, method: str, params: dict | None = None):
        data = urllib.parse.urlencode(params or {}).encode()
        req = urllib.request.Request(
            f"{self._base}/{method}", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                body = json.load(r)
        except Exception as e:
            raise BotError(f"{method}: {e}") from e
        if not body.get("ok"):
            raise BotError(f"{method}: {body}")
        return body.get("result")

    def get_updates(self, offset: int = 0, poll_sec: int = 50) -> list:
        return self.call("getUpdates", {
            "offset": offset, "timeout": poll_sec,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        }) or []

    def send(self, chat_id: int, text: str, kb: list | None = None,
             parse: str = "HTML") -> dict:
        p = {"chat_id": chat_id, "text": text, "parse_mode": parse}
        if kb is not None:
            p["reply_markup"] = json.dumps({"inline_keyboard": kb})
        return self.call("sendMessage", p)

    def edit(self, chat_id: int, msg_id: int, text: str,
             kb: list | None = None, parse: str = "HTML") -> dict | None:
        p = {"chat_id": chat_id, "message_id": msg_id, "text": text,
             "parse_mode": parse}
        if kb is not None:
            p["reply_markup"] = json.dumps({"inline_keyboard": kb})
        try:
            return self.call("editMessageText", p)
        except BotError as e:
            if "message is not modified" in str(e):
                return None
            raise

    def answer(self, cb_id: str, text: str = "", alert: bool = False) -> None:
        try:
            self.call("answerCallbackQuery",
                      {"callback_query_id": cb_id, "text": text[:200],
                       "show_alert": alert})
        except BotError:
            pass

    def delete_webhook(self) -> None:
        try:
            self.call("deleteWebhook", {"drop_pending_updates": False})
        except BotError:
            pass

    def set_webhook(self, url: str, secret: str = "") -> None:
        p = {"url": url, "drop_pending_updates": False}
        if secret:
            p["secret_token"] = secret
        self.call("setWebhook", p)
