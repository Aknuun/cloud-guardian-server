"""Linode API v4. Direct HTTPS."""
from __future__ import annotations

import json
import urllib.request

LN_API = "https://api.linode.com/v4"


class LnError(Exception):
    pass


class Ln:
    def __init__(self, token: str, timeout: int = 20):
        self._hdr = {"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"}
        self._timeout = timeout

    def _req(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(LN_API + path, data=data,
                                     headers=self._hdr, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                text = r.read().decode()
        except Exception as e:
            raise LnError(str(e)[:200]) from e
        try:
            out = json.loads(text) if text else {}
        except json.JSONDecodeError:
            out = {}
        if isinstance(out, dict) and out.get("errors"):
            errs = out["errors"]
            msg = "; ".join(str(e.get("reason", e)) for e in errs)[:200]
            raise LnError(msg)
        return out

    def call(self, path: str, method: str = "GET", body=None):
        return self._req(method, path, body)

    def all(self, path: str) -> list:
        out, page = [], 1
        sep = "&" if "?" in path else "?"
        while True:
            d = self.call(f"{path}{sep}page={page}&page_size=500")
            arr = (d or {}).get("data") or []
            out.extend(arr)
            pages = (d or {}).get("pages") or 0
            if not pages or page >= pages:
                return out
            page += 1


def status_emoji(st: str) -> str:
    return {"running": "🟢", "offline": "🔴"}.get(str(st), "🟡")


def list_text(s: dict) -> str:
    ip = ((s.get("ipv4") or ["—"])[0]) if s.get("ipv4") else "—"
    return f'{status_emoji(s.get("status"))} {s.get("label")} ({ip})'[:30]
