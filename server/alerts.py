"""Expiry/volume alerts (port of the classic pghook pipeline).

- Webhook: panels POST JSON event(s) to /pghook/<secret>.
- Urgent path: days in (0,1] / usage in [95,100) -> 3-min buffer -> one
  grouped message (per panel), 45-min gate per user/kind.
- Digest path: hourly usage / daily days full-panel scans.
- State keys mirror the classic ones (pgq:/pgdr:/pgub:/pgdlast:).
"""
from __future__ import annotations

import math
import time

PG_STATUS_FA = {"active": ("🟢", "فعال"), "expired": ("🔴", "منقضی"),
                "limited": ("🟠", "محدود"), "on_hold": ("⏸", "تعلیق"),
                "disabled": ("⛔", "غیرفعال")}

QUIET_MS = 45 * 60000
URG_WIN_MS = 3 * 60000


def get_cfg(store) -> dict:
    c = store.get_json("pghook_cfg", {}) or {}
    def _nums(v, lo, hi, dflt):
        try:
            a = [float(x) for x in (v or [])]
            a = [x for x in a if x is not None and lo <= x <= hi]
            return a[:10] or dflt
        except (ValueError, TypeError):
            return dflt
    return {
        "enabled": c.get("enabled", True) is not False,
        "days": _nums(c.get("days"), 0, 100000, [3]),
        "usage": _nums(c.get("usage"), 0.0001, 100, [90, 100]),
        "panels": c.get("panels") if isinstance(c.get("panels"), dict) else {},
        "silent_me": c.get("silent_me") is True,
    }


def save_cfg(store, cfg: dict) -> None:
    store.put_json("pghook_cfg", cfg)


def _num(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (ValueError, TypeError):
        return None


def _expire_ts(exp):
    if exp is None or exp == "" or exp == 0:
        return None
    try:
        import datetime
        if isinstance(exp, (int, float)):
            ms = exp * 1000 if exp < 1e12 else exp
            return int(ms)
        t = datetime.datetime.fromisoformat(
            str(exp).replace("Z", "+00:00")).timestamp() * 1000
        return int(t)
    except (ValueError, TypeError, OverflowError):
        return None


def parse_event(p: dict) -> dict:
    p = p or {}
    u = p.get("user") if isinstance(p.get("user"), dict) else {}
    by = p.get("by") if isinstance(p.get("by"), dict) else {}
    s = lambda v: str(v or "").strip()
    username = s(p.get("username") or p.get("user_name") or u.get("username")
                 or p.get("name"))
    owner = s(by.get("username") or p.get("admin") or p.get("owner") or
              ((u.get("admin") or {}).get("username")
               if isinstance(u.get("admin"), dict) else u.get("admin")))
    days = _num(p.get("days_left", p.get("daysLeft",
                 p.get("expire_days", p.get("remaining_days")))))
    days_explicit = days is not None
    if days is None:
        exp_ts = _expire_ts(u.get("expire", u.get("expire_date", p.get("expire"))))
        days = (exp_ts - time.time() * 1000) / 86400000 \
            if exp_ts else None
    usage = _num(p.get("usage_percent", p.get("used_percent",
                 p.get("usage_percentage"))))
    usage_explicit = usage is not None
    if usage is None:
        used = _num(u.get("used_traffic", p.get("used_traffic")))
        lim = _num(u.get("data_limit", p.get("data_limit")))
        if used is not None and lim not in (None, 0) and lim > 0:
            usage = used / lim * 100
    return {"username": username, "owner": owner,
            "ownerTg": p.get("telegram_id", by.get("telegram_id")),
            "days": days, "usage": usage,
            "daysExplicit": days_explicit, "usageExplicit": usage_explicit,
            "expireTs": _expire_ts(u.get("expire", u.get("expire_date",
                                                         p.get("expire")))),
            "status": s(u.get("status") or p.get("status"))}


def is_urgent(kind: str, value) -> bool:
    try:
        v = float(value)
    except (ValueError, TypeError):
        return False
    if kind == "days":
        return 0 < v <= 1
    if kind == "usage":
        return 95 <= v < 100
    return False


def event_jobs(ev: dict) -> list:
    out = []
    if ev.get("days") is not None:
        dv = math.floor(ev["days"] * 10) / 10
        if ev.get("daysExplicit") or (0 < dv <= 1):
            out.append({"kind": "days", "value": dv, "bucket": 1})
    if ev.get("usage") is not None:
        uv = math.floor(ev["usage"] * 10) / 10
        if ev.get("usageExplicit") or (95 <= uv < 96):
            out.append({"kind": "usage", "value": uv, "bucket": 95})
    return out


def _gate_ok(store, pid: str, username: str, kind: str, bucket) -> bool:
    raw = store.get(f"pgq:{pid}:{username}:{kind}")
    if not raw:
        return True
    try:
        ts_s, bkt = raw.rsplit(".", 1)
        age = time.time() * 1000 - float(ts_s)
    except (ValueError, TypeError):
        return True
    if age < QUIET_MS:
        return False
    return str(bkt) != str(bucket)


def _gate_mark(store, pid: str, username: str, kind: str, bucket) -> None:
    store.put(f"pgq:{pid}:{username}:{kind}",
              f"{int(time.time() * 1000)}.{bucket}", 7 * 86400)


def _fmt_line(a: dict) -> str:
    if a.get("kind") == "usage":
        return f'• {a.get("username")} — {a.get("value")}٪'
    hrs = 0
    if a.get("expireTs") and a["expireTs"] > time.time() * 1000:
        hrs = int((a["expireTs"] - time.time() * 1000) / 3600000)
    else:
        try:
            hrs = int(float(a.get("value", 0)) * 24)
        except (ValueError, TypeError):
            pass
    return f'• {a.get("username")} — انقضا {max(hrs, 0)} ساعت'


def _alert_text(panel: dict, items: list, test: bool = False) -> str:
    kinds = {a.get("kind") for a in items}
    kind = "usage" if "usage" in kinds else "days"
    head = "📊 هشدار حجم" if kind == "usage" else "🔔 هشدار انقضا"
    lines = [(("🧪 تستی — " if test else "") + head),
             f'🖥 پنل: <code>{(panel or {}).get("name", "?")}</code>', ""]
    for a in items:
        if kind == "usage" and a.get("kind") != "usage":
            continue
        if kind == "days" and a.get("kind") != "days":
            continue
        if kind == "usage":
            lines.append(f'👤 کاربر: <code>{a.get("username", "?")}</code>')
            lines.append(f'📈 حجم مصرف شده: {a.get("value")}٪')
        else:
            lines.append(f'👤 کاربر: <code>{a.get("username", "?")}</code>')
            hrs = 0
            if a.get("expireTs") and a["expireTs"] > time.time() * 1000:
                hrs = int((a["expireTs"] - time.time() * 1000) / 3600000)
            lines.append(f"⏳ انقضا در: {max(hrs, 0)} ساعت دیگر")
        if a.get("expireTs"):
            try:
                from jalali import fmt_jalali
                lines.append(f'📅 انقضا: {fmt_jalali(a["expireTs"])}')
            except (ValueError, TypeError):
                pass
        st = str(a.get("status", "")).lower()
        if st:
            em, fa = PG_STATUS_FA.get(st, ("▪️", a.get("status")))
            lines.append(f"{em} وضعیت: {fa}")
        if a.get("owner"):
            lines.append(f'👮 ادمین پنل: {a.get("owner")}')
    try:
        from jalali import fmt_jalali
        lines.append(f'⏱ {fmt_jalali(int(time.time() * 1000))}')
    except (ValueError, TypeError):
        pass
    return "\n".join(lines)


def _renew_kb(panel: dict, username: str):
    rows = []
    url_btns = []
    rb = ((panel or {}).get("renewbot") or "")
    if rb:
        url_btns.append({"text": "🔄 تمدید با ربات",
                         "url": "tg://resolve?domain=" + rb +
                                "&text=" + str(username)[:200]})
    base = str((panel or {}).get("url") or "").rstrip("/")
    if base:
        url_btns.append({"text": "🖥 تمدید با پنل",
                         "url": base + "/dashboard/#/users?search=" +
                         str(username)[:200]})
    if url_btns:
        rows.append(url_btns)
    return rows or None


def send_alert(bot, to: int, panel: dict, items: list,
               test: bool = False) -> None:
    kb = _renew_kb(panel, (items[0].get("username") if items else ""))
    bot.send(to, _alert_text(panel, items, test), kb)


def handle_events(bot, store, panel: dict, pid: str, payload,
                  admin_id: int) -> int:
    """Urgent webhook path. Returns number of buffered jobs."""
    cfg = get_cfg(store)
    if not cfg["enabled"]:
        return 0
    evs = payload if isinstance(payload, list) else [payload]
    n = 0
    for raw in evs:
        if not isinstance(raw, dict):
            continue
        ev = parse_event(raw)
        if not ev["username"]:
            continue
        batch = []
        for j in event_jobs(ev):
            if not is_urgent(j["kind"], j["value"]):
                continue
            if not _gate_ok(store, pid, ev["username"], j["kind"],
                            j["bucket"]):
                continue
            batch.append(j)
        if not batch:
            continue
        for j in batch:
            _gate_mark(store, pid, ev["username"], j["kind"], j["bucket"])
        key = f"pgub:{pid}"
        buf = store.get_json(key, {}) or {}
        if not isinstance(buf.get("items"), list):
            buf = {"ts": int(time.time() * 1000), "items": []}
        for j in batch:
            buf["items"].append({"username": ev["username"], "kind": j["kind"],
                                 "value": j["value"],
                                 "expireTs": ev.get("expireTs"),
                                 "status": ev.get("status", ""),
                                 "owner": ev.get("owner", ""),
                                 "to": [admin_id]})
            n += 1
        store.put_json(key, buf, 3600)
    flush_buffers(bot, store, {pid: panel}, admin_id, force=False)
    return n


def flush_buffers(bot, store, panels: dict, admin_id: int,
                  force: bool = False) -> int:
    cfg = get_cfg(store)
    if not cfg["enabled"]:
        return 0
    sent = 0
    for pid, panel in (panels or {}).items():
        key = f"pgub:{pid}"
        buf = store.get_json(key, {}) or {}
        items = buf.get("items") if isinstance(buf.get("items"), list) else []
        if not items:
            continue
        if not force and time.time() * 1000 - int(buf.get("ts") or 0) < URG_WIN_MS:
            continue
        store.delete(key)
        for kind in ("usage", "days"):
            part = [a for a in items if a.get("kind") == kind]
            if part:
                try:
                    send_alert(bot, admin_id, panel, part)
                    sent += 1
                except Exception:
                    pass
    return sent


def run_digest(bot, store, panels: list, kind: str, admin_id: int,
               pg_client) -> int:
    """Hourly (usage) / daily (days) full-panel digest. Returns alerts sent."""
    from pg import pct, days_left
    cfg = get_cfg(store)
    if not cfg["enabled"]:
        return 0
    sent = 0
    for p in panels or []:
        pid = str(p.get("url"))
        try:
            token = pg_client(p).login()
            users = pg_client(p).users(token)
        except Exception:
            continue
        skey = f"pgdr:{pid}:{kind}"
        st = store.get_json(skey, {}) or {}
        rep = st.get("reported") if isinstance(st.get("reported"), dict) \
            else {}
        fresh, dirty = [], False
        for u in users:
            un = u.get("username") or ""
            if not un:
                continue
            prev = rep.get(un)
            if kind == "usage":
                lim, used = float(u.get("limit") or 0), float(u.get("used") or 0)
                if not lim > 0 or used < 0:
                    continue
                pc = used / lim * 100
                if pc >= 100:
                    if prev != 100:
                        rep[un] = 100
                        dirty = True
                elif pc >= 95:
                    if prev != 95:
                        fresh.append({"username": un, "kind": "usage",
                                      "value": round(pc, 1),
                                      "expireTs": None, "status": "",
                                      "owner": u.get("owner", "")})
                        rep[un] = 95
                        dirty = True
                elif pc < 90 and prev is not None:
                    del rep[un]
                    dirty = True
            else:
                dl = days_left(u.get("expire"))
                if dl is None:
                    continue
                if dl <= 0:
                    if prev != -1:
                        rep[un] = -1
                        dirty = True
                elif dl <= 1:
                    if prev != 1:
                        fresh.append({"username": un, "kind": "days",
                                      "value": round(dl, 1),
                                      "expireTs": None, "status": "",
                                      "owner": u.get("owner", "")})
                        rep[un] = 1
                        dirty = True
                elif dl > 2 and prev is not None:
                    del rep[un]
                    dirty = True
        st["reported"] = rep
        store.put_json(skey, {"reported": rep,
                              "lastSent": st.get("lastSent", 0),
                              "lastCount": st.get("lastCount", 0)})
        for a in fresh:
            _gate_mark(store, pid, a["username"], kind,
                       95 if kind == "usage" else 1)
        if fresh:
            try:
                send_alert(bot, admin_id,
                           {"name": p.get("name"), "url": p.get("url"),
                            "renewbot": (cfg.get("panels") or {}).get(pid, {})
                            .get("renewbot", "")}, fresh)
                sent += 1
            except Exception:
                pass
        void = dirty  # state always persisted above
    lk = "pgdlast:usage" if kind == "usage" else "pgdlast:days"
    store.put_json(lk, {"ts": int(time.time() * 1000), "count": sent})
    return sent
