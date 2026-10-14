"""ArvanCloud CDN/DNS API v4. Machine-user key: Authorization: Apikey <uuid>."""
from __future__ import annotations

import json
import urllib.request

ARVAN_API = "https://napi.arvancloud.ir/cdn/4.0"


class ArvanError(Exception):
    pass


def norm_key(raw: str) -> str:
    t = str(raw or "").strip()
    if t.lower().startswith("apikey"):
        t = t[6:].strip()
    return t


class Arvan:
    def __init__(self, token: str, timeout: int = 30):
        self._hdr = {"Authorization": f"Apikey {token}",
                     "Content-Type": "application/json",
                     "Accept": "application/json"}
        self._timeout = timeout

    def _req(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(ARVAN_API + path, data=data,
                                     headers=self._hdr, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                text = r.read().decode()
        except Exception as e:
            raise ArvanError(str(e)[:200]) from e
        try:
            return json.loads(text) if text else {}
        except json.JSONDecodeError:
            return {}

    def call(self, path: str, method: str = "GET", body=None):
        return self._req(method, path, body)

    def all_pages(self, path: str, key: str = "data") -> list:
        out, page = [], 1
        sep = "&" if "?" in path else "?"
        while True:
            d = self.call(f"{path}{sep}per_page=50&page={page}")
            arr = (d or {}).get(key) or []
            out.extend(arr)
            meta = ((d or {}).get("meta") or {}).get("pagination") or {}
            if meta.get("next_page"):
                page = meta["next_page"]
                continue
            if not meta and len(arr) >= 50:
                page += 1
                continue
            return out

    def domains(self) -> list:
        return self.all_pages("/domains")

    def records(self, domain: str) -> list:
        return self.all_pages(f"/domains/{domain}/dns-records")
