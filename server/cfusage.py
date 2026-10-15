"""Cloudflare usage (GraphQL analytics + KV storage). Read-only."""
from __future__ import annotations

import datetime
import json
import urllib.request


class CfUsageError(Exception):
    pass


def _gql(token: str, query: str, timeout: int = 25):
    req = urllib.request.Request(
        "https://api.cloudflare.com/client/v4/graphql",
        data=json.dumps({"query": query}).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
    except Exception as e:
        raise CfUsageError(str(e)[:150]) from e
    if d.get("errors"):
        raise CfUsageError(str(d["errors"][0].get("message"))[:150])
    return d.get("data") or {}


def account_usage(token: str) -> dict:
    day = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    out: dict = {}
    q1 = ('{viewer{accounts{workersInvocationsAdaptive('
          f'filter:{{date_geq:"{day}",date_leq:"{day}"}},limit:10)'
          '{sum{requests}}}}}')
    try:
        d = _gql(token, q1)
        arr = d["viewer"]["accounts"][0]["workersInvocationsAdaptive"]
        out["requests"] = sum(int(x["sum"]["requests"] or 0) for x in arr)
    except (KeyError, TypeError, IndexError):
        out["requests"] = "?"
    q2 = ('{viewer{accounts{kvOperationsAdaptiveGroups(limit:50,'
          f'filter:{{date_geq:"{day}",date_leq:"{day}"}})'
          '{dimensions{actionType} sum{requests}}}}}')
    try:
        d = _gql(token, q2)
        arr = d["viewer"]["accounts"][0]["kvOperationsAdaptiveGroups"]
        kv = {}
        for g in arr:
            kv[str(g["dimensions"]["actionType"]).lower()] = \
                int(g["sum"]["requests"] or 0)
        out["kv"] = kv
    except (KeyError, TypeError, IndexError):
        out["kv"] = {}
    return out
