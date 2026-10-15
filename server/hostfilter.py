"""Automatic filtered-host swap (port of host-filter).

- Probes each panel host with check-host.net ping from Iranian nodes.
- Blocked (>= cities threshold) + resolvable -> clone DNS records under a
  new numbered name and point the panel host at it.
- Runs on schedule (interval gate) or forced; single-flight lock.
"""
from __future__ import annotations

import datetime
import json
import socket
import threading
import time
import urllib.parse
import urllib.request

CHECK_HOST = "https://check-host.net"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

IR_NODES = [
    {"id": "ir1.node.check-host.net", "city": "Tehran"},
    {"id": "ir5.node.check-host.net", "city": "Tehran"},
    {"id": "ir7.node.check-host.net", "city": "Tehran"},
    {"id": "ir8.node.check-host.net", "city": "Tehran"},
    {"id": "ir2.node.check-host.net", "city": "Isfahan"},
    {"id": "ir3.node.check-host.net", "city": "Shiraz"},
    {"id": "ir4.node.check-host.net", "city": "Shiraz"},
]
FOREIGN_NODES = [
    {"id": "de2.node.check-host.net", "city": "Frankfurt"},
    {"id": "de1.node.check-host.net", "city": "Nuremberg"},
    {"id": "nl1.node.check-host.net", "city": "Amsterdam"},
    {"id": "nl2.node.check-host.net", "city": "Meppel"},
]
IR_CITIES = ["Tehran", "Isfahan", "Shiraz"]

DEFAULTS = {"enabled": True, "intervalMin": 25, "cities": 2,
            "citiesSel": list(IR_CITIES), "maxOk": 0, "recheckCount": 1,
            "recheckMin": 1, "exceptions": [], "batch": 60,
            "maxChanges": 8, "backupKeep": 5, "realityRotate": False}

_lock = threading.Lock()


def get_cfg(store) -> dict:
    c = store.get_json("host_filter_cfg", {}) or {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in c.items() if k in DEFAULTS or
                k in ("last_run", "last_summary")})
    sel = [x for x in (out.get("citiesSel") or []) if x in IR_CITIES]
    out["citiesSel"] = sel or list(IR_CITIES)
    return out


def save_cfg(store, cfg: dict) -> None:
    cur = store.get_json("host_filter_cfg", {}) or {}
    if json.dumps(cfg, sort_keys=True) != json.dumps(cur, sort_keys=True):
        store.put_json("host_filter_cfg", cfg)


def _http(method: str, url: str, body=None, timeout: int = 25):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json",
                                          **UA}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        text = r.read().decode()
    return json.loads(text) if text else {}


def check_ping(host: str, nodes: list) -> dict:
    """{node_id: {city, ok, total}}"""
    qs = "".join(f'&node={urllib.parse.quote(n["id"])}' for n in nodes)
    rid = _http("GET", f"{CHECK_HOST}/check-ping?host="
                f"{urllib.parse.quote(host)}{qs}")
    rid = rid.get("request_id") if isinstance(rid, dict) else None
    if not rid:
        return {}
    out = {}
    for attempt in range(6):
        time.sleep(6 if attempt == 0 else 3.5)
        try:
            data = _http("GET", f"{CHECK_HOST}/check-result/{rid}",
                         timeout=20)
        except Exception:
            continue
        done = True
        for n in nodes:
            arr = (data or {}).get(n["id"])
            if not arr:
                done = False
                continue
            ok = sum(1 for a in arr if (a or [None])[0] == "OK")
            out[n["id"]] = {"city": n["city"], "ok": ok,
                            "total": len(arr)}
        if done:
            break
    return out


def blocked_cities(ping: dict, cities_sel: list, max_ok: int) -> int:
    by_city: dict = {}
    for nid, r in (ping or {}).items():
        if r.get("city") not in cities_sel:
            continue
        c = by_city.setdefault(r["city"], {"nodes": 0, "blocked": 0})
        c["nodes"] += 1
        if r.get("ok", 0) <= max_ok:
            c["blocked"] += 1
    return sum(1 for c in by_city.values()
               if c["nodes"] > 0 and c["blocked"] == c["nodes"])


def _resolve(host: str) -> list:
    try:
        return list({r[4][0] for r in socket.getaddrinfo(host, 443)})
    except OSError:
        return []


def run(bot, store, panels: list, admin_id: int, cf_accs: list,
        pg_client, force: bool = False, only: str = "") -> dict:
    """Full run. Returns summary dict. Never raises."""
    res = {"checked": 0, "changed": 0, "msg": ""}
    if not _lock.acquire(blocking=False):
        return {"checked": 0, "changed": 0, "msg": "run in progress"}
    try:
        return _run(bot, store, panels, admin_id, cf_accs, pg_client,
                    force, only, res)
    except Exception as e:
        res["msg"] = str(e)[:200]
        return res
    finally:
        _lock.release()


def _run(bot, store, panels, admin_id, cf_accs, pg_client, force,
         only, res) -> dict:
    import datetime as _dt
    cfg = get_cfg(store)
    now_ms = int(time.time() * 1000)
    if not cfg.get("enabled") and not force:
        return res
    if not force:
        try:
            last = int(_dt.datetime.fromisoformat(
                str(cfg.get("last_run") or "2000-01-01")).timestamp() * 1000)
        except ValueError:
            last = 0
        if last and now_ms - last < max(1, int(cfg.get("intervalMin", 25))) * 60000:
            return res
    changed, checked = 0, 0
    for p in panels or []:
        try:
            c = pg_client(p)
            token = c.login()
            hosts = c.hosts(token)
        except Exception:
            continue
        for h in hosts or []:
            domains = []
            for field in ("address", "sni", "host"):
                v = (h.get("host") or {}).get(field)
                vals = v if isinstance(v, list) else ([v] if v else [])
                domains.extend(str(x) for x in vals if x)
            domains = sorted(set(domains))
            for dv in domains[:int(cfg.get("batch", 60))]:
                if only and only != dv:
                    continue
                if dv in (cfg.get("exceptions") or []):
                    continue
                checked += 1
                ping = check_ping(dv, [n for n in IR_NODES
                                       if n["city"] in cfg["citiesSel"]])
                if blocked_cities(ping, cfg["citiesSel"],
                                  int(cfg.get("maxOk", 0))) < int(cfg.get("cities", 2)):
                    continue
                if not _resolve(dv):
                    continue
                ok = True
                for _ in range(int(cfg.get("recheckCount", 1))):
                    time.sleep(max(1, int(cfg.get("recheckMin", 1))) * 60)
                    ping2 = check_ping(dv, [n for n in IR_NODES
                                            if n["city"] in cfg["citiesSel"]])
                    if blocked_cities(ping2, cfg["citiesSel"],
                                      int(cfg.get("maxOk", 0))) < int(cfg.get("cities", 2)):
                        ok = False
                        break
                if not ok:
                    continue
                if changed >= int(cfg.get("maxChanges", 8)):
                    break
                if _swap(bot, p, token, h, dv, cf_accs, admin_id):
                    changed += 1
            if changed >= int(cfg.get("maxChanges", 8)):
                break
    cfg["last_run"] = _dt.datetime.now(
        _dt.timezone.utc).isoformat()
    cfg["last_summary"] = f"بررسی {checked} دامنه — {changed} تعویض."
    save_cfg(store, cfg)
    res.update(checked=checked, changed=changed)
    return res


def _swap(bot, panel, panel_token: str, host, old_name: str, cf_accs,
          admin_id: int) -> bool:
    """Create numbered clone + repoint panel host. Returns True on change."""
    from cf import Cf
    low = old_name.lower()
    for a in cf_accs or []:
        try:
            cli = Cf(a["token"])
            zones = [z for z in cli.zones() or []
                     if z.get("status") == "active"]
        except Exception:
            continue
        for z in zones:
            zn = str(z.get("name", "")).lower()
            if low != zn and not low.endswith("." + zn):
                continue
            rel = low[:-(len(zn) + 1)] if low != zn else ""
            try:
                recs = cli.records(z["id"]) or []
            except Exception:
                continue
            names = {str(r.get("name", "")).lower() for r in recs}
            new_name = None
            for n in range(1, 61):
                cand = f"{rel}{n}.{zn}" if rel else f"h{n}.{zn}"
                if cand not in names:
                    new_name = cand
                    break
            if not new_name:
                continue
            src = [r for r in recs
                   if str(r.get("name", "")).lower() == low
                   and r.get("type") in ("A", "AAAA", "CNAME")]
            try:
                if src:
                    for r in src:
                        cli.add_record(z["id"], r["type"], new_name,
                                       r.get("content"),
                                       r.get("ttl", 1) or 1,
                                       bool(r.get("proxied")))
                else:
                    cli.add_record(z["id"], "CNAME", new_name, old_name,
                                   1, False)
                repl = {low: new_name}
                body = dict(host.get("host") or {})
                for field in ("address", "sni", "host"):
                    v = body.get(field)
                    if isinstance(v, list):
                        body[field] = [repl.get(str(x).lower(), x)
                                       for x in v]
                    elif v:
                        body[field] = repl.get(str(v).lower(), v)
                body.pop("id", None)
                import urllib.request as _u
                import json as _j
                req = _u.Request(
                    panel["url"].rstrip("/") + f'/api/host/{host.get("id")}',
                    data=_j.dumps(body).encode(),
                    headers={"Authorization": f"Bearer {panel_token}",
                             "Content-Type": "application/json",
                             **UA},
                    method="PUT")
                with _u.urlopen(req, timeout=30):
                    pass
            except Exception as e:
                try:
                    bot.send(admin_id, f"❌ تعویض {old_name} نشد: {e}"[:300])
                except Exception:
                    pass
                return False
            try:
                bot.send(admin_id,
                         f"🔄 تعویض خودکار\n<code>{old_name}</code> → "
                         f"<code>{new_name}</code>")
            except Exception:
                pass
            return True
    return False
