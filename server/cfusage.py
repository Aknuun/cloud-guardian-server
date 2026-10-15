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


def fetch_traffic_counts(token: str, zone_id: str, days: int = 7) -> dict:
    """DNS query counts per name over the last N days.
    Returns {counts:{name:count}} or {need_perm:True} or {error:str}."""
    days = days if days in (1, 7, 30) else 7
    now = datetime.datetime.now(datetime.timezone.utc)
    start = now - datetime.timedelta(days=days)
    fmt = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
    q = ('{viewer{zones(filter:{zoneTag:"' + zone_id + '"})'
         '{dnsAnalyticsAdaptiveGroups(limit:1000,'
         f'filter:{{datetime_geq:"{fmt(start)}",datetime_leq:"{fmt(now)}"}},'
         'orderBy:[count_DESC]){count dimensions{queryName}}}}}')
    try:
        d = _gql(token, q)
    except CfUsageError as e:
        msg = str(e)
        if __import__("re").search(r"permission|authz|analytics\.read", msg, 2):
            return {"need_perm": True}
        return {"error": msg[:200]}
    if d.get("errors"):
        msg = str((d["errors"][0] or {}).get("message") or "")
        if __import__("re").search(r"permission|authz|analytics\.read", msg, 2):
            return {"need_perm": True}
        return {"error": msg[:200]}
    try:
        z = d["viewer"]["zones"][0]
        groups = z.get("dnsAnalyticsAdaptiveGroups") or []
    except (KeyError, TypeError, IndexError):
        return {"counts": {}}
    counts: dict = {}
    for g in groups:
        qn = str((g.get("dimensions") or {}).get("queryName") or "")
        qn = qn.lower().rstrip(".")
        if qn:
            counts[qn] = counts.get(qn, 0) + int(g.get("count") or 0)
    return {"counts": counts}


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
