"""Hetzner Cloud API. Direct HTTPS — no relay needed on-server."""
from __future__ import annotations

import json
import time
import urllib.request

HZ_API = "https://api.hetzner.cloud/v1"


class HzError(Exception):
    pass


_FA = [
    ("unsupported_location", "این پلن در لوکیشن انتخاب‌شده موجود نیست."),
    ("server_limit", "محدودیت تعداد سرور این اکانت پر شده است."),
    ("resource_limit_exceeded", "محدودیت منابع اکانت پر شده است."),
    ("unauthorized", "توکن هتزنر نامعتبر است."),
    ("resource_locked", "ریسورس قفل است؛ چند لحظه بعد دوباره تلاش کن."),
    ("uniqueness_error", "نام انتخاب‌شده تکراری است."),
    ("invalid_input", "ورودی نامعتبر است."),
    ("not_found", "موردی پیدا نشد."),
    ("insufficient", "موجودی/پرداخت اکانت کافی نیست."),
]


def err_fa(payload) -> str:
    err = (payload or {}).get("error") or {}
    code = str(err.get("code", ""))
    msg = str(err.get("message", ""))
    s = (code + " " + msg).lower()
    for key, fa in _FA:
        if key in s:
            return f"{fa}\n({msg})" if msg else fa
    return msg or "خطای نامشخص هتزنر"


class Hz:
    def __init__(self, token: str, timeout: int = 20):
        self._hdr = {"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"}
        self._timeout = timeout

    def _req(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(HZ_API + path, data=data,
                                     headers=self._hdr, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                text = r.read().decode()
        except Exception as e:
            raise HzError(str(e)[:200]) from e
        try:
            return json.loads(text) if text else {}
        except json.JSONDecodeError:
            return {}

    def call(self, path: str, method: str = "GET", body=None, retry_lock=True):
        last = None
        for k in range(4):
            out = self._req(method, path, body)
            err = (out or {}).get("error") or {}
            if retry_lock and err.get("code") == "resource_locked" and k < 3:
                time.sleep(1.5 * (k + 1))
                last = out
                continue
            if err:
                raise HzError(err_fa(out))
            return out
        raise HzError(err_fa(last))

    def all(self, path: str) -> list:
        out, page = [], 1
        sep = "&" if "?" in path else "?"
        while True:
            d = self.call(f"{path}{sep}per_page=50&page={page}")
            key = next((k for k in (d or {}) if isinstance(d[k], list)), None)
            arr = d[key] if key else []
            out.extend(arr)
            pg = (d.get("meta") or {}).get("pagination") or {}
            if pg.get("next_page"):
                page = pg["next_page"]
                continue
            if not pg and len(arr) >= 50:
                page += 1
                continue
            return out


def status_emoji(st: str) -> str:
    return {"running": "🟢", "off": "🔴", "starting": "🟡"}.get(
        str(st), "⚪")


def gb(num) -> float:
    try:
        return round(float(num or 0) / 1024 ** 3, 3)
    except (ValueError, TypeError):
        return 0.0


def age_days(iso) -> str:
    if not iso:
        return "—"
    try:
        import datetime
        ms = datetime.datetime.fromisoformat(
            str(iso).replace("Z", "+00:00")).timestamp() * 1000
        return str(max(0, int((__import__("time").time() * 1000 - ms) / 86400000)))
    except (ValueError, TypeError):
        return "—"


_euro_cache: dict = {"at": 0, "v": None}


def euro_rate() -> float | None:
    import time
    if time.time() - _euro_cache["at"] < 6 * 3600 and _euro_cache["v"]:
        return _euro_cache["v"]
    try:
        req = urllib.request.Request("https://sarfe.erfjab.com/api/prices")
        with urllib.request.urlopen(req, timeout=15) as r:
            rate = float((json.load(r) or {}).get("eur1") or 0)
        if rate > 0:
            _euro_cache.update(at=time.time(), v=rate)
            return rate
    except Exception:
        pass
    return _euro_cache["v"]


def price_fmt(eur, euro) -> str:
    try:
        v = float(eur)
    except (ValueError, TypeError):
        return "—"
    s = (f"{v:.4f}" if v < 1 else f"{v:.2f}") + "€"
    if euro:
        s += f" [{round(v * euro):,}T]".replace(",", ",")
    return s


def snap_count(images: list, server_id) -> int:
    try:
        sid = int(server_id)
    except (ValueError, TypeError):
        return 0
    return sum(1 for im in images or []
               if (im or {}).get("type") == "snapshot"
               and ((im.get("created_from") or {}).get("id")) == sid)


def primary_ip_id(v):
    if not v:
        return None
    return v.get("id") if isinstance(v, dict) else v


def ensure_off(hz: "Hz", server_id, max_ms: int = 30000) -> bool:
    import time
    try:
        s0 = (hz.call(f"/servers/{server_id}") or {}).get("server") or {}
        if s0.get("status") != "off":
            hz.call(f"/servers/{server_id}/actions/poweroff", "POST", {})
        t0 = time.time() * 1000
        while time.time() * 1000 - t0 < max_ms:
            s = (hz.call(f"/servers/{server_id}") or {}).get("server") or {}
            if not s.get("locked") and s.get("status") == "off":
                return True
            time.sleep(1.5)
    except HzError:
        pass
    return False
