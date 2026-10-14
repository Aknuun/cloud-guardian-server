"""Minimal Telegram Bot API client (stdlib only).

Long-polling receiver + send/edit helpers. No third-party deps so the
server install stays lean. Every network call runs in a daemon thread
with a hard join-timeout, so even a DNS/connect stall (getaddrinfo is
NOT bounded by socket timeouts) can never freeze the bot; it just
logs and moves on.
"""
from __future__ import annotations

import json
import socket
import threading
import urllib.parse
import urllib.request


class BotError(Exception):
    pass


def _prefer_ipv4() -> None:
    # این مسیر گاهی روی IPv6 stall می‌خورد ولی curl (eyeballs) رد می‌شود؛
    # پس getaddrinfo را طوری می‌چینیم که IPv4 اول امتحان شود.
    _orig = socket.getaddrinfo

    def _patched(host, port, *a, **k):
        try:
            res = _orig(host, port, *a, **k)
        except Exception:
            raise
        v4 = [r for r in res if r[0] == socket.AF_INET]
        rest = [r for r in res if r[0] != socket.AF_INET]
        return v4 + rest

    socket.getaddrinfo = _patched


try:
    _prefer_ipv4()
except Exception:
    pass


CALL_TIMEOUT = 40

# رنگ‌بندی دکمه‌ها (مثل کد قبلی — تلگرام style را رنگی رندر می‌کند):
#   • ردیف‌های خطرناک (حذف/لغو/بازگشت/خانه/...) → قرمز (danger)
#   • ردیف‌های «لیست داده» (دامنه/رکورد/سرور/...) → بدون رنگ
#   • بقیه: ۱ و ۳ ستون → سبز · ۲ و ۴+ ستون → آبی
_DATA_CB = ("cfz:", "cfr:", "hzs:", "hzsi:", "pgl:", "pgu:", "addrec:",
            "zf:", "cfp:", "sel:", "e:", "p:", "z:", "dd:", "sr:", "qanz:",
            "hzsv:", "hzn:")
_DANGER_CB = ("del", "delete", "dely", "dy:", "dy", "stop", "cancel",
              "reset", "revoke")
_DANGER_WORDS = ("حذف", "لغو", "انصراف", "غیرفعال", "توقف", "ریست",
                 "بازنشانی", "پاک‌کردن", "بازگشت")


def _is_placeholder(b) -> bool:
    return (not isinstance(b, dict)) or b.get("text") == " "


def _is_back(b) -> bool:
    if not isinstance(b, dict):
        return False
    t, cb = str(b.get("text", "")), str(b.get("callback_data", ""))
    if any(e in t for e in ("🏠", "🔙", "⬅️", "بازگشت", "منو")):
        return True
    return cb in ("menu", "back", "noop") and ("🏠" in t or "🔙" in t or
                                               "بازگشت" in t or "منو" in t)


def _row_is_danger(row) -> bool:
    for b in row:
        if not isinstance(b, dict) or _is_placeholder(b):
            continue
        if b.get("style") == "danger":
            return True
        cb, t = str(b.get("callback_data", "")), str(b.get("text", ""))
        if any(k in cb for k in _DANGER_CB):
            return True
        if any(w in t for w in ("🗑", "🛑", "🚫") + _DANGER_WORDS):
            return True
    return False


def _row_is_data(row) -> bool:
    return any(isinstance(b, dict) and not _is_placeholder(b)
               and str(b.get("callback_data", "")).startswith(_DATA_CB)
               for b in row)


def _style_row(row: list) -> list:
    if not isinstance(row, list):
        return row
    if _row_is_data(row):
        out = []
        for b in row:
            if _is_placeholder(b):
                out.append(b)
            elif _is_back(b):
                out.append({**b, "style": "danger"})
            elif isinstance(b, dict) and b.get("style") == "plain":
                d = dict(b)
                d.pop("style", None)
                out.append(d)
            else:
                out.append(b)
        return out
    if _row_is_danger(row):
        auto = "danger"
    else:
        n = sum(1 for b in row
                if isinstance(b, dict) and not _is_placeholder(b))
        auto = "success" if n in (1, 3) else "primary"
    out = []
    for b in row:
        if _is_placeholder(b):
            out.append(b)
        elif _is_back(b):
            out.append({**b, "style": "danger"})
        elif isinstance(b, dict) and b.get("style"):
            if b["style"] == "plain":
                d = dict(b)
                d.pop("style", None)
                out.append(d)
            else:
                out.append(b)
        elif isinstance(b, dict):
            out.append({**b, "style": auto})
        else:
            out.append(b)
    return out


def style_kb(kb: list | None) -> list | None:
    if not kb:
        return kb
    return [_style_row(row) for row in kb]


class Bot:
    def __init__(self, token: str, timeout: int = 60):
        if not token or ":" not in token:
            raise ValueError("bad bot token")
        self._base = f"https://api.telegram.org/bot{token}"
        self._timeout = timeout

    def _raw(self, method: str, params: dict | None = None):
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

    def call(self, method: str, params: dict | None = None,
             timeout: int = CALL_TIMEOUT):
        # hard timeout even against DNS/connect stalls
        out: dict = {}
        def _run():
            try:
                out["r"] = self._raw(method, params)
            except Exception as e:
                out["e"] = e
        th = threading.Thread(target=_run, daemon=True)
        th.start()
        th.join(timeout=timeout)
        if th.is_alive():
            raise BotError(f"{method}: stall>={timeout}s")
        if "e" in out:
            raise out["e"]
        return out.get("r")

    def get_updates(self, offset: int = 0, poll_sec: int = 50) -> list:
        return self.call("getUpdates", {
            "offset": offset, "timeout": poll_sec,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        }, timeout=poll_sec + 20) or []

    def send(self, chat_id: int, text: str, kb: list | None = None,
             parse: str = "HTML") -> dict:
        p = {"chat_id": chat_id, "text": text, "parse_mode": parse}
        if kb is not None:
            p["reply_markup"] = json.dumps({"inline_keyboard": style_kb(kb)})
        return self.call("sendMessage", p)

    def edit(self, chat_id: int, msg_id: int, text: str,
             kb: list | None = None, parse: str = "HTML") -> dict | None:
        p = {"chat_id": chat_id, "message_id": msg_id, "text": text,
             "parse_mode": parse}
        if kb is not None:
            p["reply_markup"] = json.dumps({"inline_keyboard": style_kb(kb)})
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
