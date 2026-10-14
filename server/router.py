"""Update router: menus, callbacks and wizards.

Conventions (kept tiny on purpose):
- callback data: <section>:<args...> (e.g. cfz:<zone_id>)
- per-chat UI session:  s:<chat_id>  (JSON, 1h TTL)
- pending text input:   pend:<chat_id> (JSON, 15min TTL)
- every handler returns (text, keyboard) or sends directly.
"""
from __future__ import annotations

import html

from cf import Cf, CfError
from hz import Hz, HzError, status_emoji
from store import Store
from tg import Bot

VERSION = "server-1.0.0"

PAGE = 30


def esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def home_kb() -> list:
    return [[btn("🏠 خانه", "menu")]]


class Router:
    def __init__(self, bot: Bot, store: Store, cf: Cf, admin_id: int):
        self.bot = bot
        self.store = store
        self.cf = cf
        self.admin_id = admin_id

    # ---------- entry ----------

    def handle(self, upd: dict) -> None:
        try:
            if "callback_query" in upd:
                self._on_callback(upd["callback_query"])
            elif "message" in upd:
                self._on_message(upd["message"])
        except Exception as e:
            try:
                chat = self._chat_of(upd)
                if chat:
                    self.bot.send(chat, f"❌ خطای داخلی: {esc(str(e))[:200]}",
                                  home_kb())
            except Exception:
                pass

    @staticmethod
    def _chat_of(upd: dict):
        if "callback_query" in upd:
            m = upd["callback_query"].get("message") or {}
            return (m.get("chat") or {}).get("id")
        m = upd.get("message") or {}
        return (m.get("chat") or {}).get("id")

    # ---------- messages ----------

    def _on_message(self, m: dict) -> None:
        chat = (m.get("chat") or {}).get("id")
        frm = (m.get("from") or {}).get("id")
        text = str(m.get("text") or "").strip()
        if not chat or frm != self.admin_id:
            return
        pend = self.store.get_json(f"pend:{chat}")
        if pend and not text.startswith("/"):
            self._wizard_step(chat, m.get("message_id", 0), pend, text)
            return
        if text.startswith("/start") or text == "🏠 خانه":
            self._menu(chat)
        elif text.startswith("/cf"):
            self._cf_zones(chat, edit=False)
        else:
            self._menu(chat)

    # ---------- callbacks ----------

    def _on_callback(self, cb: dict) -> None:
        data = str(cb.get("data") or "")
        msg = cb.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        mid = msg.get("message_id", 0)
        if not chat or (cb.get("from") or {}).get("id") != self.admin_id:
            return
        self.bot.answer(cb.get("id", ""))
        if data == "menu":
            self._menu(chat, mid)
        elif data == "hz":
            self._hz_home(chat, mid)
        elif data == "hza":
            self.store.put_json(f"pend:{chat}", {"type": "hz_add_name"}, 600)
            self._show(chat, mid, "👤 چه اسمی برای اکانت هتزنر بزارم؟",
                       [[btn("🔙 بازگشت", "hz"), btn("🏠 خانه", "menu")]])
        elif data == "hzdel":
            self._hz_del_list(chat, mid)
        elif data.startswith("hzd:"):
            self._hz_del_ask(chat, mid, data[4:])
        elif data.startswith("hzdy:"):
            self._hz_del_do(chat, mid, data[5:])
        elif data.startswith("hzm:"):
            accs = self._hz_accs()
            try:
                i = int(data[4:])
                if 0 <= i < len(accs):
                    self.store.put("hz_sel", str(i))
            except (ValueError, IndexError):
                pass
            self._hz_home(chat, mid)
        elif data == "hzaccs":
            self._hz_accs_menu(chat, mid)
        elif data.startswith("hzs:"):
            _, i, pg = data.split(":")
            self._hz_servers(chat, mid, int(i), int(pg))
        elif data.startswith("hzsi:"):
            _, i, sid = data.split(":")
            self._hz_info(chat, mid, int(i), sid)
        elif data.startswith("hzsu:"):
            _, i, sid, step = data.split(":")
            self._hz_op(chat, mid, int(i), sid, step)
        elif data.startswith("hzdelok:"):
            _, i, sid = data.split(":")
            self._hz_del_server(chat, mid, int(i), sid)
        elif data == "cf":
            self._cf_zones(chat, mid)
        elif data.startswith("cfz:"):
            self._cf_records(chat, mid, data[4:], 0)
        elif data.startswith("cfp:"):
            _, zid, pg = data.split(":", 2)
            self._cf_records(chat, mid, zid, int(pg))
        elif data.startswith("cfr:"):
            rid = data[4:]
            zid = self._cf_zid(chat)
            if not zid:
                self._show(chat, mid, "⏳ نشست منقضی شد؛ دامنه را دوباره باز کن.",
                           home_kb())
            else:
                self._cf_record(chat, mid, zid, rid)
        elif data.startswith("cfdel:"):
            rid = data[6:]
            zid = self._cf_zid(chat)
            if not zid:
                self._show(chat, mid, "⏳ نشست منقضی شد؛ دامنه را دوباره باز کن.",
                           home_kb())
            else:
                self._cf_del_ask(chat, mid, zid, rid)
        elif data.startswith("cfdelok:"):
            rid = data[8:]
            zid = self._cf_zid(chat)
            if not zid:
                self._show(chat, mid, "⏳ نشست منقضی شد؛ دامنه را دوباره باز کن.",
                           home_kb())
            else:
                self._cf_del_do(chat, mid, zid, rid)
        elif data.startswith("cfpx:"):
            rid = data[5:]
            zid = self._cf_zid(chat)
            if zid:
                self._cf_toggle_px(chat, mid, zid, rid)
        elif data.startswith("cfttl:"):
            rid = data[6:]
            zid = self._cf_zid(chat)
            if zid:
                self._cf_cycle_ttl(chat, mid, zid, rid)
        elif data.startswith("cfedit:"):
            rid = data[7:]
            zid = self._cf_zid(chat)
            if not zid:
                self._show(chat, mid, "⏳ نشست منقضی شد؛ دامنه را دوباره باز کن.",
                           home_kb())
                return
            self.store.put_json(f"pend:{chat}",
                                {"type": "cfedit", "zid": zid, "rid": rid}, 900)
            self._show(chat, mid, "✏️ مقدار جدید را بفرستید:",
                       [[btn("❌ انصراف", f"cfr:{rid}")]])
        elif data.startswith("cfadd:"):
            self._cf_add_start(chat, mid, data[6:])
        else:
            self.bot.answer(cb.get("id", ""), "نامشخص")

    # ---------- rendering ----------

    def _show(self, chat: int, mid: int, text: str, kb: list) -> None:
        if mid:
            try:
                if self.bot.edit(chat, mid, text, kb) is not None:
                    return
            except Exception:
                pass
        self.bot.send(chat, text, kb)

    def _menu(self, chat: int, mid: int = 0) -> None:
        kb = [
            [btn("☁️ کلودفلر", "cf")],
            [btn("🇩🇪 هتزنر", "hz")],
            [btn("🏠 خانه", "menu")],
        ]
        self._show(chat, mid,
                   f"🤖 نگهبان ابری <b>{VERSION}</b> (روی سرور)\n\n"
                   "DNS کلادفلر و هتزنر کامل است؛ بقیه بخش‌ها به‌تدریج می‌آیند.",
                   kb)

    # ---------- Cloudflare ----------

    def _cf_zones(self, chat: int, mid: int = 0, edit: bool = True) -> None:
        try:
            zones = [z for z in self.cf.zones()
                     if z.get("status") == "active"]
        except CfError as e:
            self._show(chat, mid, f"❌ خطای کلادفلر: {esc(e)}", home_kb())
            return
        rows = [[btn(z["name"], f'cfz:{z["id"]}')] for z in zones[:40]]
        rows.append([btn("🏠 خانه", "menu")])
        self._show(chat, mid,
                   f"☁️ کلودفلر — {len(zones)} دامنه فعال", rows)

    def _cf_zid(self, chat: int) -> str:
        # آی‌دی ۳۲کاراکتری زون در callback جا نمی‌شود (سقف ۶۴ بایت تلگرام)؛
        # پس در نشست نگه می‌داریم و دکمه‌ها فقط آی‌دی رکورد را حمل می‌کنند.
        try:
            sess = self.store.get_json(f"s:{chat}", {})
            return str((sess or {}).get("zid") or "")
        except Exception:
            return ""

    def _cf_records(self, chat: int, mid: int, zid: str, pg: int) -> None:
        try:
            recs = self.cf.records(zid) or []
            zones = self.cf.zones()
        except CfError as e:
            self._show(chat, mid, f"❌ خطای کلادفلر: {esc(e)}", home_kb())
            return
        zname = next((z["name"] for z in zones if z["id"] == zid), zid)
        self.store.put_json(f"s:{chat}", {"zid": zid}, 3600)
        pages = max(1, (len(recs) + PAGE - 1) // PAGE)
        pg = max(0, min(pg, pages - 1))
        rows = []
        for r in recs[pg * PAGE:(pg + 1) * PAGE]:
            short = str(r.get("name", ""))
            if short.endswith("." + zname):
                short = short[:-(len(zname) + 1)] or "@"
            rows.append([btn(f'{short}-{r.get("type")}',
                             f'cfr:{r.get("id")}')])
        nav = []
        if pages > 1:
            nav.append(btn(f"{pg + 1}/{pages}",
                           f"cfp:{zid}:{(pg + 1) % pages}"))
        if nav:
            rows.append(nav)
        rows.append([btn("➕ افزودن", f"cfadd:{zid}"),
                     btn("🔙 بازگشت", "cf")])
        rows.append([btn("🏠 خانه", "menu")])
        self._show(chat, mid,
                   f"📋 <b>{esc(zname)}</b> — صفحه {pg + 1} از {pages} "
                   f"({len(recs)} رکورد)", rows)

    def _cf_record(self, chat: int, mid: int, zid: str, rid: str) -> None:
        try:
            recs = self.cf.records(zid) or []
        except CfError as e:
            self._show(chat, mid, f"❌ خطای کلادفلر: {esc(e)}", home_kb())
            return
        r = next((x for x in recs if x.get("id") == rid), None)
        if not r:
            self._show(chat, mid, "❌ رکورد پیدا نشد.", home_kb())
            return
        txt = (f"📝 <b>{esc(r.get('name'))}</b>\n"
               f"نوع: {esc(r.get('type'))}\n"
               f"مقدار: <code>{esc(r.get('content'))}</code>\n"
               f"TTL: {esc(r.get('ttl'))} · پروکسی: "
               f"{'روشن' if r.get('proxied') else 'خاموش'}")
        kb = [[btn("🗑 حذف", f"cfdel:{rid}"),
               btn("🔙 بازگشت", f"cfz:{zid}")],
              [btn(f'🌐 پروکسی: {"روشن" if r.get("proxied") else "خاموش"}',
                   f"cfpx:{rid}"),
               btn(f'⏱ TTL: {r.get("ttl")}', f"cfttl:{rid}")],
              [btn("✏️ تغییر مقدار", f"cfedit:{rid}")],
              [btn("🏠 خانه", "menu")]]
        self._show(chat, mid, txt, kb)

    def _cf_del_ask(self, chat: int, mid: int, zid: str, rid: str) -> None:
        kb = [[btn("✅ بله، حذف کن", f"cfdelok:{rid}"),
               btn("❌ انصراف", f"cfr:{rid}")]]
        self._show(chat, mid, "⚠️ مطمئنی حذف شود؟", kb)

    def _cf_del_do(self, chat: int, mid: int, zid: str, rid: str) -> None:
        try:
            self.cf.del_record(zid, rid)
            self._cf_records(chat, mid, zid, 0)
        except CfError as e:
            self._show(chat, mid, f"❌ حذف نشد: {esc(e)}", home_kb())

    def _cf_find(self, zid: str, rid: str):
        try:
            recs = self.cf.records(zid) or []
        except CfError:
            return None
        return next((x for x in recs if x.get("id") == rid), None)

    def _cf_toggle_px(self, chat: int, mid: int, zid: str, rid: str) -> None:
        r = self._cf_find(zid, rid)
        if not r:
            self._show(chat, mid, "❌ رکورد پیدا نشد.", home_kb())
            return
        try:
            self.cf.update_record(zid, r, proxied=not r.get("proxied"))
        except CfError as e:
            self._show(chat, mid, f"❌ نشد: {esc(e)}", home_kb())
            return
        self._cf_record(chat, mid, zid, rid)

    def _cf_cycle_ttl(self, chat: int, mid: int, zid: str, rid: str) -> None:
        r = self._cf_find(zid, rid)
        if not r:
            self._show(chat, mid, "❌ رکورد پیدا نشد.", home_kb())
            return
        steps = [1, 120, 300, 600, 1800, 3600]
        try:
            cur = int(r.get("ttl", 1))
        except (ValueError, TypeError):
            cur = 1
        nxt = steps[0] if cur not in steps else steps[(steps.index(cur) + 1) % len(steps)]
        try:
            self.cf.update_record(zid, r, ttl=nxt)
        except CfError as e:
            self._show(chat, mid, f"❌ نشد: {esc(e)}", home_kb())
            return
        self._cf_record(chat, mid, zid, rid)

    def _cf_add_start(self, chat: int, mid: int, zid: str) -> None:
        self.store.put_json(f"pend:{chat}",
                            {"type": "cfadd", "zid": zid, "step": "rec",
                             "back": f"cfz:{zid}"}, 900)
        self._show(chat, mid,
                   "➕ رکورد جدید — نوع و نام و مقدار را با فاصله بفرست:\n"
                   "<code>A sub 1.2.3.4</code>",
                   [[btn("❌ انصراف", f"cfz:{zid}")]])

    def _wizard_step(self, chat: int, mid: int, pend: dict, text: str) -> None:
        if pend.get("type") == "cfadd":
            parts = text.split()
            if len(parts) < 3:
                self.bot.send(chat, "❌ قالب درست نیست. مثال: <code>A sub 1.2.3.4</code>")
                return
            rtype, name, content = parts[0].upper(), parts[1], parts[2]
            try:
                self.cf.add_record(pend["zid"], rtype, name, content)
            except CfError as e:
                self.bot.send(chat, f"❌ ثبت نشد: {esc(e)}")
                return
            self.store.delete(f"pend:{chat}")
            self.bot.send(chat, "✅ رکورد ثبت شد.")
            self._cf_records(chat, 0, pend["zid"], 0)
        elif pend.get("type") == "cfedit":
            r = self._cf_find(pend["zid"], pend["rid"])
            if not r:
                self.store.delete(f"pend:{chat}")
                self.bot.send(chat, "❌ رکورد پیدا نشد.")
                return
            try:
                self.cf.update_record(pend["zid"], r, content=text.strip())
            except CfError as e:
                self.bot.send(chat, f"❌ نشد: {esc(e)}")
                return
            self.store.delete(f"pend:{chat}")
            self.bot.send(chat, "✅ مقدار عوض شد.")
            self._cf_record(chat, 0, pend["zid"], pend["rid"])
        elif pend.get("type") == "hz_add_name":
            name = text.replace(" ", "")
            if not name:
                self.bot.send(chat, "❌ نام معتبر نیست.")
                return
            self.store.put_json(f"pend:{chat}",
                                {"type": "hz_add_token", "name": name}, 600)
            self.bot.send(chat, "🔑 توکن API هتزنر را بفرستید:")
        elif pend.get("type") == "hz_add_token":
            tok = text.strip()
            try:
                Hz(tok).all("/servers")
            except HzError as e:
                self.bot.send(chat, f"❌ توکن نامعتبر است: {esc(e)}")
                return
            accs = self._hz_accs()
            accs.append({"name": pend.get("name", "hz"), "token": tok})
            self.store.put_json("hz_accounts", accs)
            self.store.delete(f"pend:{chat}")
            self.bot.send(chat, "✅ اکانت هتزنر اضافه شد.")
            self._hz_home(chat, 0)
        elif pend.get("type") == "hz_srv_rename":
            try:
                Hz(self._hz_accs()[pend["acc"]]["token"]).call(
                    f'/servers/{pend["sid"]}', "PUT", {"name": text.strip()})
                self.bot.send(chat, "✅ نام سرور تغییر کرد.")
            except (HzError, IndexError, KeyError) as e:
                self.bot.send(chat, f"❌ نشد: {esc(e)}")
                return
            self.store.delete(f"pend:{chat}")
            self._hz_info(chat, 0, pend["acc"], pend["sid"])
        else:
            self.store.delete(f"pend:{chat}")

    # ---------- Hetzner ----------

    def _hz_accs(self) -> list:
        accs = self.store.get_json("hz_accounts", [])
        return accs if isinstance(accs, list) else []

    def _hz_sel(self) -> int:
        accs = self._hz_accs()
        try:
            i = int(self.store.get("hz_sel") or 0)
            if 0 <= i < len(accs):
                return i
        except (ValueError, TypeError):
            pass
        return 0

    def _hz_home(self, chat: int, mid: int = 0) -> None:
        accs = self._hz_accs()
        if not accs:
            self._show(chat, mid, "🇩🇪 هتزنر\n\nاکانتی نیست؛ اضافه کن:",
                       [[btn("➕ افزودن اکانت", "hza")],
                        [btn("🏠 خانه", "menu")]])
            return
        sel = self._hz_sel()
        kb = []
        if len(accs) > 1:
            kb.append([btn(f'{"✅ " if i == sel else "👤 "}{a["name"]}',
                           f"hzm:{i}") for i, a in enumerate(accs)][:4])
        try:
            servers = Hz(accs[sel]["token"]).all("/servers")
        except HzError as e:
            self._show(chat, mid, f"❌ {esc(e)}",
                       [[btn("🏠 خانه", "menu")]])
            return
        lines = [f'🇩🇪 هتزنر — «{esc(accs[sel]["name"])}»', ""]
        if not servers:
            lines.append("📭 سروری نیست.")
        for s in servers[:20]:
            ip = ((s.get("public_net") or {}).get("ipv4") or {}).get("ip", "—")
            kb.append([btn(f'🌐 {ip}', f'hzsi:{sel}:{s["id"]}'),
                       btn(f'{status_emoji(s.get("status"))} '
                           f'{str(s.get("name") or s["id"])[:28]}',
                           f'hzsi:{sel}:{s["id"]}')])
        kb.append([btn("🖥 سرورها", f"hzs:{sel}:0"),
                   btn("👤 اکانت‌ها", "hzaccs")])
        kb.append([btn("🏠 خانه", "menu")])
        self._show(chat, mid, "\n".join(lines), kb)

    def _hz_accs_menu(self, chat: int, mid: int = 0) -> None:
        accs = self._hz_accs()
        kb = [[btn("➕ افزودن اکانت", "hza")]]
        if accs:
            kb.append([btn("🗑 حذف اکانت", "hzdel")])
        kb.append([btn("🔙 بازگشت", "hz"), btn("🏠 خانه", "menu")])
        self._show(chat, mid, "👤 اکانت‌های هتزنر:", kb)

    def _hz_del_list(self, chat: int, mid: int = 0) -> None:
        accs = self._hz_accs()
        if not accs:
            self._show(chat, mid, "📭 اکانتی نیست.",
                       [[btn("🔙 بازگشت", "hz")]])
            return
        kb = [[btn(f'🗑 {a["name"]}', f'hzd:{i}')]
              for i, a in enumerate(accs)]
        kb.append([btn("🔙 بازگشت", "hz")])
        self._show(chat, mid, "🗑 کدام اکانت حذف شود؟", kb)

    def _hz_del_ask(self, chat: int, mid: int, idx: str) -> None:
        try:
            name = self._hz_accs()[int(idx)]["name"]
        except (ValueError, IndexError):
            self._show(chat, mid, "❌ اکانت پیدا نشد.", home_kb())
            return
        self._show(chat, mid, f'⚠️ اکانت «{esc(name)}» حذف شود؟',
                   [[btn("✅ بله", f"hzdy:{idx}"),
                     btn("❌ انصراف", "hzdel")]])

    def _hz_del_do(self, chat: int, mid: int, idx: str) -> None:
        accs = self._hz_accs()
        try:
            accs.pop(int(idx))
        except (ValueError, IndexError):
            self._show(chat, mid, "❌ اکانت پیدا نشد.", home_kb())
            return
        self.store.put_json("hz_accounts", accs)
        self.bot.send(chat, "✅ اکانت حذف شد.")
        self._hz_home(chat, 0)

    def _hz_servers(self, chat: int, mid: int, i: int, pg: int) -> None:
        try:
            acc = self._hz_accs()[i]
            servers = Hz(acc["token"]).all("/servers")
        except (IndexError, HzError) as e:
            self._show(chat, mid, f"❌ {esc(e)}", home_kb())
            return
        per, pages = 10, max(1, (len(servers) + 9) // 10)
        pg = max(0, min(pg, pages - 1))
        kb = [[btn(f'{status_emoji(s.get("status"))} {s.get("name")}',
                   f'hzsi:{i}:{s["id"]}')]
              for s in servers[pg * per:(pg + 1) * per]]
        if pages > 1:
            kb.append([btn(f"{pg + 1}/{pages}", f"hzs:{i}:{(pg + 1) % pages}")])
        kb.append([btn("🔙 بازگشت", "hz"), btn("🏠 خانه", "menu")])
        self._show(chat, mid,
                   f'🖥 سرورهای «{esc(acc["name"])}» ({len(servers)}):', kb)

    def _hz_info(self, chat: int, mid: int, i: int, sid: str) -> None:
        try:
            acc = self._hz_accs()[i]
            s = Hz(acc["token"]).call(f"/servers/{sid}").get("server") or {}
        except (IndexError, HzError) as e:
            self._show(chat, mid, f"❌ {esc(e)}", home_kb())
            return
        if not s:
            self._show(chat, mid, "❌ سرور پیدا نشد.", home_kb())
            return
        ip4 = ((s.get("public_net") or {}).get("ipv4") or {}).get("ip", "—")
        ip6 = ((s.get("public_net") or {}).get("ipv6") or {}).get("ip", "—")
        st = s.get("server_type") or {}
        txt = (f'🚀 <b>{esc(s.get("name"))}</b> [{esc(s.get("status"))}]\n'
               f"IPv4: <code>{esc(ip4)}</code>\n"
               f"IPv6: <code>{esc(ip6)}</code>\n"
               f'CPU: {esc(st.get("cores", "—"))} · RAM: {esc(st.get("memory", "—"))}GB · '
               f'Disk: {esc(st.get("disk", "—"))}GB')
        kb = [[btn("⚡ روشن", f"hzsu:{i}:{sid}:on"),
               btn("🔌 خاموش", f"hzsu:{i}:{sid}:off")],
              [btn("🔄 ریبوت", f"hzsu:{i}:{sid}:reboot"),
               btn("✏️ تغییر نام", f"hzsu:{i}:{sid}:rename")],
              [btn("🗑 حذف سرور", f"hzsu:{i}:{sid}:del")],
              [btn("🔙 بازگشت", f"hzs:{i}:0"),
               btn("🏠 خانه", "menu")]]
        self._show(chat, mid, txt, kb)

    def _hz_op(self, chat: int, mid: int, i: int, sid: str, step: str) -> None:
        try:
            tok = self._hz_accs()[i]["token"]
        except IndexError:
            self._show(chat, mid, "❌ اکانت پیدا نشد.", home_kb())
            return
        if step == "rename":
            self.store.put_json(f"pend:{chat}",
                                {"type": "hz_srv_rename", "acc": i,
                                 "sid": sid}, 600)
            self._show(chat, mid, "✏️ نام جدید سرور را بفرستید:",
                       [[btn("🔙 انصراف", f"hzsi:{i}:{sid}")]])
            return
        if step == "del":
            self._show(chat, mid, "⚠️ سرور حذف شود؟",
                       [[btn("✅ بله، حذف کن", f"hzdelok:{i}:{sid}"),
                         btn("❌ انصراف", f"hzsi:{i}:{sid}")]])
            return
        action = {"on": "poweron", "off": "poweroff",
                  "reboot": "reboot", "reset": "reset"}.get(step)
        if not action:
            self._show(chat, mid, "❌ عملیات نامشخص.", home_kb())
            return
        try:
            Hz(tok).call(f"/servers/{sid}/actions/{action}", "POST", {})
            self.bot.send(chat, "✅ دستور ارسال شد.")
        except HzError as e:
            self._show(chat, mid, f"❌ نشد: {esc(e)}", home_kb())
            return
        self._hz_info(chat, 0, i, sid)

    def _hz_del_server(self, chat: int, mid: int, i: int, sid: str) -> None:
        try:
            tok = self._hz_accs()[i]["token"]
            Hz(tok).call(f"/servers/{sid}", "DELETE")
            self.bot.send(chat, "✅ سرور حذف شد.")
        except (IndexError, HzError) as e:
            self._show(chat, mid, f"❌ نشد: {esc(e)}", home_kb())
            return
        self._hz_servers(chat, 0, i, 0)
