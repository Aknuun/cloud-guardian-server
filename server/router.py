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
        elif data == "cf":
            self._cf_zones(chat, mid)
        elif data.startswith("cfz:"):
            self._cf_records(chat, mid, data[4:], 0)
        elif data.startswith("cfp:"):
            _, zid, pg = data.split(":", 2)
            self._cf_records(chat, mid, zid, int(pg))
        elif data.startswith("cfr:"):
            _, zid, rid = data.split(":", 2)
            self._cf_record(chat, mid, zid, rid)
        elif data.startswith("cfdel:"):
            _, zid, rid = data.split(":", 2)
            self._cf_del_ask(chat, mid, zid, rid)
        elif data.startswith("cfdelok:"):
            _, zid, rid = data.split(":", 2)
            self._cf_del_do(chat, mid, zid, rid)
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
            [btn("🏠 خانه", "menu")],
        ]
        self._show(chat, mid,
                   f"🤖 نگهبان ابری <b>{VERSION}</b> (روی سرور)\n\n"
                   "بخش‌ها به‌تدریج اضافه می‌شوند؛ الان DNS کلادفلر کامل است.",
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

    def _cf_records(self, chat: int, mid: int, zid: str, pg: int) -> None:
        try:
            recs = self.cf.records(zid) or []
            zones = self.cf.zones()
        except CfError as e:
            self._show(chat, mid, f"❌ خطای کلادفلر: {esc(e)}", home_kb())
            return
        zname = next((z["name"] for z in zones if z["id"] == zid), zid)
        pages = max(1, (len(recs) + PAGE - 1) // PAGE)
        pg = max(0, min(pg, pages - 1))
        rows = []
        for r in recs[pg * PAGE:(pg + 1) * PAGE]:
            short = str(r.get("name", ""))
            if short.endswith("." + zname):
                short = short[:-(len(zname) + 1)] or "@"
            rows.append([btn(f'{short}-{r.get("type")}',
                             f'cfr:{zid}:{r.get("id")}')])
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
        kb = [[btn("🗑 حذف", f"cfdel:{zid}:{rid}"),
               btn("🔙 بازگشت", f"cfz:{zid}")],
              [btn("🏠 خانه", "menu")]]
        self._show(chat, mid, txt, kb)

    def _cf_del_ask(self, chat: int, mid: int, zid: str, rid: str) -> None:
        kb = [[btn("✅ بله، حذف کن", f"cfdelok:{zid}:{rid}"),
               btn("❌ انصراف", f"cfr:{zid}:{rid}")]]
        self._show(chat, mid, "⚠️ مطمئنی حذف شود؟", kb)

    def _cf_del_do(self, chat: int, mid: int, zid: str, rid: str) -> None:
        try:
            self.cf.del_record(zid, rid)
            self._cf_records(chat, mid, zid, 0)
        except CfError as e:
            self._show(chat, mid, f"❌ حذف نشد: {esc(e)}", home_kb())

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
        else:
            self.store.delete(f"pend:{chat}")
