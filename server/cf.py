"""Cloudflare API (Zones + DNS). HTTPS only — no relay needed on-server."""
from __future__ import annotations

import json
import urllib.request

CF_API = "https://api.cloudflare.com/client/v4"


class CfError(Exception):
    pass


class Cf:
    def __init__(self, token: str, timeout: int = 25):
        self._hdr = {"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"}
        self._timeout = timeout

    def _req(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(CF_API + path, data=data,
                                     headers=self._hdr, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                out = json.load(r)
        except Exception as e:
            raise CfError(str(e)[:200]) from e
        if not out.get("success"):
            errs = out.get("errors") or [{}]
            raise CfError(str(errs[0].get("message", errs))[:200])
        return out.get("result")

    def zones(self) -> list:
        return self._req("GET", "/zones?per_page=50") or []

    def records(self, zone_id: str) -> list:
        return self._req("GET", f"/zones/{zone_id}/dns_records?per_page=100") or []

    def add_record(self, zone_id: str, rtype: str, name: str, content: str,
                   ttl: int = 1, proxied: bool = False,
                   priority: int | None = None) -> dict:
        body = {"type": rtype, "name": name, "content": content,
                "ttl": ttl, "proxied": proxied}
        if priority is not None:
            body["priority"] = priority
        return self._req("POST", f"/zones/{zone_id}/dns_records", body)

    def del_record(self, zone_id: str, rec_id: str) -> dict:
        return self._req("DELETE", f"/zones/{zone_id}/dns_records/{rec_id}")

    def update_record(self, zone_id: str, rec: dict, **changes) -> dict:
        body = {"type": rec.get("type"), "name": rec.get("name"),
                "content": rec.get("content"), "ttl": rec.get("ttl", 1),
                "proxied": bool(rec.get("proxied"))}
        body.update(changes)
        return self._req("PUT", f"/zones/{zone_id}/dns_records/{rec.get('id')}",
                         body)
