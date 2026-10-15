"""Passargad/Marzban panel API (read-only management). Direct HTTPS."""
from __future__ import annotations

import datetime
import json
import urllib.parse
import urllib.request


class PgError(Exception):
    pass


UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _get(url: str, token: str, timeout: int = 25):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", **UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                raise PgError(f"HTTP {r.status}")
            return json.load(r)
    except PgError:
        raise
    except Exception as e:
        raise PgError(str(e)[:200]) from e


class Pg:
    def __init__(self, url: str, username: str, password: str):
        self.base = url.rstrip("/")
        self.username = username
        self.password = password

    def login(self) -> str:
        data = urllib.parse.urlencode(
            {"username": self.username, "password": self.password}).encode()
        req = urllib.request.Request(
            self.base + "/api/admin/token", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded", **UA})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.load(r)
        except Exception as e:
            raise PgError(str(e)[:200]) from e
        tok = (d or {}).get("access_token")
        if not tok:
            raise PgError("ورود ناموفق (یوزر/پسورد؟)")
        return tok

    def users(self, token: str, limit: int = 1000) -> list:
        out, offset = [], 0
        for _ in range(6):
            d = _get(f"{self.base}/api/users?limit={limit}&offset={offset}",
                     token)
            arr = d.get("users") if isinstance(d, dict) else None
            if not isinstance(arr, list) or not arr:
                break
            for u in arr:
                if not isinstance(u, dict):
                    continue
                adm = u.get("admin")
                owner = (adm or {}).get("username") if isinstance(adm, dict) \
                    else (adm or "-")
                out.append({
                    "username": str(u.get("username") or u.get("id") or ""),
                    "used": float(u.get("used_traffic") or 0),
                    "limit": float(u.get("data_limit") or 0),
                    "expire": u.get("expire", u.get("expire_date")),
                    "status": str(u.get("status") or ""),
                    "owner": str(owner or "-"),
                })
            offset += len(arr)
            if offset >= int((d or {}).get("total") or 0):
                break
        return out

    def users_since(self, token: str, since_iso: str) -> list:
        out, offset = [], 0
        for _ in range(6):
            d = _get(f"{self.base}/api/users?online_after="
                     f"{urllib.parse.quote(since_iso)}&limit=5000&offset={offset}",
                     token, timeout=30)
            arr = d.get("users") if isinstance(d, dict) else None
            if not isinstance(arr, list) or not arr:
                break
            for u in arr:
                if not isinstance(u, dict):
                    continue
                adm = u.get("admin")
                owner = adm.get("username") if isinstance(adm, dict) \
                    else (adm or "-")
                out.append({"id": str(u.get("id") or u.get("username") or ""),
                            "username": str(u.get("username") or ""),
                            "used": float(u.get("used_traffic") or 0),
                            "owner": str(owner or "-")})
            offset += len(arr)
        return out

    def nodes(self, token: str) -> list:
        d = _get(f"{self.base}/api/nodes", token, timeout=15)
        arr = d if isinstance(d, list) else (d or {}).get("nodes") or \
            (d or {}).get("items") or (d or {}).get("result") or []
        out = []
        for n in arr:
            if not isinstance(n, dict):
                continue
            out.append({"name": str(n.get("name") or n.get("node_name")
                                     or n.get("id") or ""),
                        "status": str(n.get("status") or "unknown"),
                        "address": n.get("address"),
                        "version": n.get("xray_version") or n.get("version"),
                        "reason": n.get("message") or n.get("error")})
        return out

    def settings(self, token: str) -> dict | None:
        try:
            d = _get(f"{self.base}/api/settings", token, timeout=25)
        except PgError:
            return None
        return d if isinstance(d, dict) else None

    def hosts(self, token: str) -> list:
        d = _get(f"{self.base}/api/hosts", token, timeout=25)
        return d if isinstance(d, list) else []

    def user(self, token: str, username: str) -> dict | None:
        try:
            d = _get(f"{self.base}/api/user/{urllib.parse.quote(username)}",
                     token, timeout=20)
        except PgError:
            return None
        return d if isinstance(d, dict) else None


GB = 1024 ** 3


def gb(v) -> float:
    try:
        return round(float(v or 0) / GB, 2)
    except (ValueError, TypeError):
        return 0.0


def pct(used: float, limit: float):
    try:
        if limit and limit > 0:
            return round(used / limit * 100, 1)
    except (ValueError, TypeError, ZeroDivisionError):
        pass
    return None


def days_left(expire):
    try:
        if expire is None or expire == "" or expire == 0:
            return None
        ms = None
        if isinstance(expire, (int, float)):
            ms = expire * 1000 if expire < 1e12 else expire
        elif isinstance(expire, str):
            t = datetime.datetime.fromisoformat(expire.replace("Z", "+00:00"))
            ms = t.timestamp() * 1000
        if ms is None:
            return None
        return round((ms - datetime.datetime.now().timestamp() * 1000) / 86400000, 1)
    except (ValueError, TypeError):
        return None
