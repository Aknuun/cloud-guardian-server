"""Snapshot push: server -> failover Worker's KV (via Cloudflare API).

Only tokens + tiny metadata (no user data). Every 10 minutes so the
failover worker never works with data older than ~10 min.
"""
from __future__ import annotations

import json
import time
import urllib.request


def push_snapshot(cf_api_token: str, account_id: str, namespace_id: str,
                  snap: dict, timeout: int = 25) -> bool:
    body = json.dumps({"cf_token": snap.get("cf_token", ""),
                       "hz_token": snap.get("hz_token", ""),
                       "ts": int(time.time())}).encode()
    url = (f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
           f"/storage/kv/namespaces/{namespace_id}/values/fo_snap")
    req = urllib.request.Request(
        url, data=body,
        headers={"Authorization": f"Bearer {cf_api_token}"},
        method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False
