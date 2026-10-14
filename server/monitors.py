"""Usage monitor (bad-consumption) + node monitor (Passargad panels)."""
from __future__ import annotations

import datetime
import time

UM_DEFAULTS = {"enabled": True, "hourlyGb": 7, "dailyGb": 25,
               "cooldownH": 6, "activeMin": 75, "minGb": 1, "report": True}
UM_MIN_MS = 60 * 60000
UM_RECENT_KEEP = 6
UM_RECENT_WINDOW_MS = 70 * 60000
UM_DAY_MIN_MS = 23 * 3600000
UM_DAY_MAX_MS = 48 * 3600000
UM_REPORT_MS = 24 * 3600000

NODE_UP = {"connected", "up", "online", "running", "healthy"}
NODE_DOWN = {"error", "down", "offline", "stopped", "unreachable", "off"}
ND_ALERT_COOLDOWN_MS = 30 * 60000
ND_QUIET_MS = 10 * 60000


def node_dir(status: str) -> str:
    s = str(status or "").lower()
    if s in NODE_UP:
        return "up"
    if s in NODE_DOWN:
        return "down"
    return "mid"


def um_cfg(store) -> dict:
    c = store.get_json("usage_monitor_cfg", {}) or {}
    out = dict(UM_DEFAULTS)
    out.update(c)
    return out


def um_save_cfg(store, cfg: dict) -> None:
    store.put_json("usage_monitor_cfg", cfg)


def _find_sample(recent: list, target_ms: int, max_diff_ms: int):
    best, best_diff = None, None
    for s in recent or []:
        try:
            d = abs(float(s.get("t", 0)) - target_ms)
        except (ValueError, TypeError):
            continue
        if d <= max_diff_ms and (best_diff is None or d < best_diff):
            best, best_diff = s, d
    return best


def _pattern(recent: list, uid: str) -> bool:
    vals = []
    for s in recent or []:
        v = (s.get("u") or {}).get(uid)
        if v is not None:
            try:
                vals.append(float(v))
            except (ValueError, TypeError):
                pass
    if len(vals) < 3:
        return False
    active = sum(1 for i in range(1, len(vals)) if vals[i] - vals[i - 1] > 0)
    return active >= min(4, len(vals) - 1)


def run_usage_monitor(bot, store, panels: list, admin_id: int,
                      pg_client) -> int:
    """Returns alerts sent. pg_client(panel) -> obj with login()/users()."""
    from jalali import fmt_jalali
    cfg = um_cfg(store)
    if not cfg.get("enabled"):
        return 0
    now = int(time.time() * 1000)
    last = int(store.get("um_last_run") or 0)
    if last and now - last < UM_MIN_MS:
        return 0
    store.put("um_last_run", str(now))
    if not panels:
        return 0
    since = (datetime.datetime.now(datetime.timezone.utc) -
             datetime.timedelta(minutes=float(cfg.get("activeMin", 75))))
    since_iso = since.isoformat()
    hour_th = float(cfg.get("hourlyGb", 7)) * 1024 ** 3
    day_th = float(cfg.get("dailyGb", 25)) * 1024 ** 3
    cool_ms = float(cfg.get("cooldownH", 6)) * 3600000
    min_b = float(cfg.get("minGb", 1)) * 1024 ** 3
    rep_ts = int(store.get("um_report_ts") or 0)
    report_due = bool(cfg.get("report")) and rep_ts > 0 and \
        now - rep_ts >= UM_REPORT_MS
    sent = 0
    for p in panels:
        pid = str(p.get("url"))
        try:
            c = pg_client(p)
            token = c.login()
            users = c.users_since(token, since_iso)
        except Exception:
            continue
        if not users:
            continue
        st = store.get_json(f"um:{pid}", {}) or {}
        recent = st.get("recent") if isinstance(st.get("recent"), list) else []
        cool = st.get("cool") if isinstance(st.get("cool"), dict) else {}
        day = st.get("day") if isinstance(st.get("day"), dict) else None
        day_age = now - int(day.get("t", 0)) if day else 0
        day_ready = bool(day) and UM_DAY_MIN_MS <= day_age <= 30 * 3600000
        cur = {}
        for u in users:
            try:
                if min_b and float(u.get("used", 0)) < min_b:
                    continue
                cur[str(u.get("id"))] = float(u.get("used", 0))
            except (ValueError, TypeError):
                continue
        hour_old = _find_sample(recent, now - 60000, 35000)
        alerts = []
        for u in users:
            uid = str(u.get("id"))
            try:
                used = float(u.get("used", 0))
            except (ValueError, TypeError):
                continue
            best = None
            if hour_old and (hour_old.get("u") or {}).get(uid) is not None:
                try:
                    d = used - float(hour_old["u"][uid])
                except (ValueError, TypeError):
                    d = 0
                if d > 0 and d >= hour_th:
                    best = {"gb": d / 1024 ** 3, "label": "ساعت اخیر"}
            if day_ready and (day.get("u") or {}).get(uid) is not None:
                try:
                    d = used - float(day["u"][uid])
                except (ValueError, TypeError):
                    d = 0
                if d > 0 and d >= day_th:
                    g = d / 1024 ** 3
                    if best is None or g > best["gb"]:
                        best = {"gb": g, "label": "۲۴ ساعت اخیر"}
            if best is None:
                continue
            if cool_ms and now - float(cool.get(uid, 0)) < cool_ms:
                continue
            cool[uid] = now
            flat = _pattern(recent, uid)
            log = store.get_json("um_alerts_log", []) or []
            log.append({"t": now, "panel": p.get("name"),
                        "username": u.get("username"), "owner": u.get("owner"),
                        "gb": best["gb"], "label": best["label"]})
            cutoff = now - 3 * 24 * 3600000
            log = [x for x in log if x.get("t", 0) >= cutoff][-2000:]
            store.put_json("um_alerts_log", log)
            alerts.append({"username": u.get("username"),
                           "owner": u.get("owner"), **best, "flat": flat})
        if alerts:
            alerts.sort(key=lambda a: -a["gb"])
            lines = ["🚨 مصرف بالا (احتمال پخش لینک)",
                     f'🕐 {fmt_jalali(now)}', ""]
            for a in alerts[:30]:
                lines.append(f'▪️ <code>{a["username"]}</code> — '
                             f'{a["gb"]:.1f} GB ({a["label"]})')
                lines.append(f'   ادمین: {a["owner"]} • '
                             f'{"مصرف پیوسته 🔁" if a["flat"] else "مصرف جهشی ⚡"}')
            if len(alerts) > 30:
                lines.append(f"… و {len(alerts) - 30} کاربر دیگر")
            try:
                bot.send(admin_id, "\n".join(lines))
                sent += 1
            except Exception:
                pass
        recent.append({"t": now, "u": cur})
        recent = [s for s in recent if now - s.get("t", 0) <= UM_RECENT_WINDOW_MS][-UM_RECENT_KEEP:]
        if not day or day_ready or day_age > UM_DAY_MAX_MS:
            day = {"t": now, "u": cur}
        for uid in list(cool):
            try:
                if now - float(cool[uid]) > 3 * 24 * 3600000:
                    del cool[uid]
            except (ValueError, TypeError):
                del cool[uid]
        store.put_json(f"um:{pid}", {"recent": recent, "day": day,
                                     "cool": cool})
    if report_due:
        log = store.get_json("um_alerts_log", []) or []
        warned = [x for x in log if x.get("t", 0) >= now - UM_REPORT_MS]
        if warned:
            by_user = {}
            for w in warned:
                g = float(w.get("gb") or 0)
                prev = by_user.get(w.get("username"))
                if not prev:
                    by_user[w.get("username")] = {
                        "username": w.get("username"),
                        "owner": w.get("owner") or "—",
                        "panel": w.get("panel") or "—",
                        "gb": g, "label": w.get("label") or ""}
                elif w.get("label") == "۲۴ ساعت اخیر":
                    prev.update(gb=g, label=w.get("label"),
                                owner=w.get("owner") or prev["owner"],
                                panel=w.get("panel") or prev["panel"])
                elif g > prev["gb"] and prev["label"] != "۲۴ ساعت اخیر":
                    prev["gb"] = g
                    prev["label"] = w.get("label")
            arr = sorted(by_user.values(), key=lambda x: -x["gb"])[:30]
            lines = ["📊 گزارش پرمصرف‌های اخطارشده (۲۴ ساعت اخیر)",
                     f'🕐 {fmt_jalali(now)}', ""]
            for i, t in enumerate(arr, 1):
                lines.append(f'{i}. <code>{t["username"]}</code> — '
                             f'{t["gb"]:.1f} GB ({t["label"]})')
                lines.append(f'   ادمین: {t["owner"]} • 🖥 {t["panel"]}')
            try:
                bot.send(admin_id, "\n".join(lines))
                sent += 1
            except Exception:
                pass
        store.put("um_report_ts", str(now))
    elif not rep_ts:
        store.put("um_report_ts", str(now))
    return sent


# ---------------- node monitor ----------------

def get_node_monitors(store) -> list:
    lst = store.get_json("node_monitors", [])
    return lst if isinstance(lst, list) else []


def _node_state(store, mid: str) -> dict:
    s = store.get_json(f"ndst:{mid}", {}) or {}
    return s if isinstance(s.get("nodes"), dict) else {"nodes": {}, "ts": None}


def _apply_poll(prev_nodes: dict, lst: list, now_iso: str) -> dict:
    nxt, events, seen = {}, [], set()
    new = False
    for n in lst or []:
        name = str((n or {}).get("name") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        prev = (prev_nodes or {}).get(name)
        d = node_dir((n or {}).get("status"))
        if not prev:
            new = True
        else:
            pd = node_dir(prev.get("status"))
            if pd and pd != d and d in ("up", "down"):
                events.append({"name": name, "dir": d, "node": n})
        nxt[name] = {"name": name, "status": (n or {}).get("status"),
                     "address": (n or {}).get("address"),
                     "version": (n or {}).get("version"),
                     "reason": (n or {}).get("reason"),
                     "ts": now_iso,
                     "first_seen": (prev or {}).get("first_seen") or now_iso,
                     "last_alert": (prev or {}).get("last_alert"),
                     "last_dir": (prev or {}).get("last_dir")}
    return {"next": nxt, "events": events, "hasNew": new}


def _should_alert(prev_node: dict, now_ms: int, direction: str) -> bool:
    try:
        t = int(datetime.datetime.fromisoformat(
            str((prev_node or {}).get("last_alert") or "").replace("Z", "+00:00")
        ).timestamp() * 1000) if (prev_node or {}).get("last_alert") else 0
    except (ValueError, TypeError):
        t = 0
    if not t > 0:
        return True
    if direction == "up" and (prev_node or {}).get("last_dir") != "up":
        return True
    return now_ms - t >= ND_ALERT_COOLDOWN_MS


def run_node_poll(bot, store, panels: list, admin_id: int, pg_client,
                  force: bool = False) -> int:
    """Returns alerts sent."""
    from jalali import fmt_jalali
    mons = get_node_monitors(store)
    if not panels or not mons:
        return 0
    by_panel = {str(p.get("url")): p for p in panels}
    sent, now_ms = 0, int(time.time() * 1000)
    now_iso = datetime.datetime.now(
        datetime.timezone.utc).isoformat()
    tokens = {}
    for m in mons:
        if m.get("enabled") is False:
            continue
        interval = max(1, int(m.get("intervalMin") or 10))
        st = _node_state(store, m.get("id"))
        if not force:
            tick = now_ms // 60000
            try:
                last = int(datetime.datetime.fromisoformat(
                    str(st.get("ts") or "2000-01-01").replace("Z", "+00:00")
                ).timestamp() * 1000)
            except (ValueError, TypeError):
                last = 0
            if not (tick % interval == 0 and now_ms - last >= interval * 60000):
                continue
        panel = by_panel.get(str(m.get("panel_url")))
        if panel is None:
            continue
        if panel["url"] not in tokens:
            try:
                c = pg_client(panel)
                tokens[panel["url"]] = (c, c.login())
            except Exception:
                continue
        c, token = tokens[panel["url"]]
        try:
            lst = c.nodes(token)
        except Exception:
            continue
        poll = _apply_poll(st.get("nodes"), lst, now_iso)
        dirty, fired = poll["hasNew"], False
        for ev in poll["events"]:
            if ev["name"] in (m.get("excluded") or []):
                continue
            dirty = True
            prev = (st.get("nodes") or {}).get(ev["name"]) or {}
            if not _should_alert(prev, now_ms, ev["dir"]):
                continue
            gk = f'ndq:{panel.get("url")}:{ev["name"]}:{ev["dir"]}'
            try:
                last_g = float(store.get(gk) or 0)
            except (ValueError, TypeError):
                last_g = 0
            if now_ms - last_g < ND_QUIET_MS:
                continue
            store.put(gk, str(now_ms), 3600)
            n, name = ev["node"] or {}, ev["name"]
            ts = fmt_jalali(now_ms)
            if ev["dir"] == "down":
                msg = (f"🚨 نود قطع شد!\n🖥 پنل: <code>{panel.get('name')}</code>\n"
                       f'🖧 نود: <code>{name}</code>'
                       + (f'\n🌐 آیپی: <code>{n.get("address")}</code>'
                          if n.get("address") else "")
                       + f"\n⏱ زمان: {ts} به وقت ایران\n"
                         f'🔎 علت: {n.get("reason") or "—"}')
            else:
                msg = (f"✅ نود وصل شد!\n🖥 پنل: <code>{panel.get('name')}</code>\n"
                       f"🖧 نود: <code>{name}</code>\n⏱ زمان: {ts} به وقت ایران")
            try:
                bot.send(admin_id, msg)
                sent += 1
                fired = True
                poll["next"][name]["last_alert"] = now_iso
                poll["next"][name]["last_dir"] = ev["dir"]
            except Exception:
                pass
        for name, nd in poll["next"].items():
            st.setdefault("nodes", {})[name] = nd
        try:
            last_save = int(datetime.datetime.fromisoformat(
                str(st.get("ts") or "2000-01-01").replace("Z", "+00:00")
            ).timestamp() * 1000)
        except (ValueError, TypeError):
            last_save = 0
        if dirty and (fired or now_ms - last_save >= 5 * 60000):
            st["ts"] = now_iso
            store.put_json(f'ndst:{m.get("id")}', st)
    return sent
