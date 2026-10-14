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
from pg import Pg, PgError, gb, pct, days_left
from store import Store
from tg import Bot

VERSION = "server-1.0.0"

PAGE = 30

# 📌 قانون چیدمان (مثل کد قبلی): دکمه‌ها همیشه کنار هم در ردیف؛
# متن کوتاه → ۳/۴ ستونه؛ فقط متن خیلی بلند تک‌ستونه؛ تأیید دوتایی کنار هم.
EMPTY_BTN = {"text": " ", "callback_data": "noop"}


def _grid(btns: list, cols: int) -> list:
    rows = []
    for i in range(0, len(btns), cols):
        row = list(btns[i:i + cols])
        while len(row) < cols:
            row.append(EMPTY_BTN)
        rows.append(row)
    return rows


def grid2(btns: list) -> list:
    return _grid(btns, 2)


def grid3(btns: list) -> list:
    return _grid(btns, 3)


def grid4(btns: list) -> list:
    return _grid(btns, 4)


SOON = "⏳ این بخش در نسخه سروری به‌زودی می‌آید."

SEARCH_PROMPT_TEXT = (
    "🔍 جست‌وجو\n\n"
    "در هر بخش از ربات با وارد کردن آی‌پی یا دامنه، افزودن سریع یا "
    "جست‌وجو انجام می‌شود.\n\n"
    "آی‌پی یا دامنه یا بخشی از آن را بفرستید:"
)


def esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def home_kb() -> list:
    return [[btn("🏠 خانه", "menu")]]


class Router:
    def __init__(self, bot: Bot, store: Store, cf: Cf, admin_id: int,
                 cf_token: str = ""):
        self.bot = bot
        self.store = store
        self.cf = cf
        self.cf_token = cf_token
        self.admin_id = admin_id

    # ---------- helpers (Cloudflare accounts / favs / labels) ----------

    def _cf_accs(self) -> list:
        accs = self.store.get_json("cf_accounts", None)
        if isinstance(accs, list) and accs:
            return accs
        if self.cf_token:
            return [{"name": "main", "token": self.cf_token}]
        return []

    def _cf_client(self, i: int) -> Cf:
        return Cf(self._cf_accs()[i]["token"])

    def _favs(self) -> list:
        favs = self.store.get_json("favs", [])
        return favs if isinstance(favs, list) else []

    @staticmethod
    def _ttl_label(ttl) -> str:
        try:
            t = int(ttl)
        except (ValueError, TypeError):
            return "خودکار"
        if t in (1, 120):
            return "خودکار"
        if t < 3600:
            return f"{t // 60} دقیقه"
        return f"{t // 3600} ساعت"

    @staticmethod
    def _rec_label(name: str, rtype: str, zname: str) -> str:
        if zname and name.endswith("." + zname):
            name = name[:-(len(zname) + 1)] or "@"
        return f"{name}-{str(rtype or 'A').upper()}"

    @staticmethod
    def _dup_labels(recs: list, zname: str) -> dict:
        from collections import Counter
        counts = Counter(str(r.get("name", "")) for r in recs)
        seen: dict = {}
        out: dict = {}
        for r in recs:
            label = Router._rec_label(str(r.get("name", "")),
                                      r.get("type"), zname)
            if counts[str(r.get("name", ""))] > 1:
                seen[r.get("name")] = seen.get(r.get("name"), 0) + 1
                label = f"{seen[r.get('name')]}. {label}"
            out[r.get("id")] = label
        return out

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
        elif text.startswith("/zones"):
            self._cf_zones(chat)
        elif text.startswith("/search"):
            self.store.put_json(f"pend:{chat}", {"type": "gsearch"}, 600)
            self._show(chat, 0, SEARCH_PROMPT_TEXT,
                       [[btn("❌ انصراف", "menu")]])
        elif text.startswith("/favorites"):
            self._favs_page(chat)
        elif text.startswith("/newrecord"):
            self._addrec_zones(chat)
        elif text.startswith("/cf"):
            self._cf_zones(chat)
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
        elif data == "pg" or data == "pndef":
            self._pg_home(chat, mid)
        elif data == "pga":
            self.store.put_json(f"pend:{chat}", {"type": "pg_add_url"}, 600)
            self._show(chat, mid, "🔗 آدرس پنل را بفرستید (مثل https://panel.example.com):",
                       [[btn("🔙 بازگشت", "pg"), btn("🏠 خانه", "menu")]])
        elif data == "pgdel":
            self._pg_del_list(chat, mid)
        elif data.startswith("pgd:"):
            self._pg_del_ask(chat, mid, data[4:])
        elif data.startswith("pgdy:"):
            self._pg_del_do(chat, mid, data[5:])
        elif data.startswith("pgl:"):
            _, i, pg = data.split(":")
            self._pg_users(chat, mid, int(i), int(pg), "")
        elif data.startswith("pgu:"):
            self._pg_user(chat, mid, int(data[4:]))
        elif data == "pgq":
            sess = self.store.get_json(f"s:{chat}", {}) or {}
            if "panel" not in sess:
                self._show(chat, mid, "اول یک پنل را باز کن.", home_kb())
            else:
                self.store.put_json(f"pend:{chat}",
                                    {"type": "pg_search",
                                     "panel": sess["panel"]}, 600)
                self._show(chat, mid, "🔍 بخشی از یوزرنیم را بفرستید:",
                           [[btn("❌ انصراف", f'pgl:{sess["panel"]}:0')]])
        elif data == "cf" or data == "zones":
            self._cf_zones(chat, mid)
        elif data.startswith("zf:"):
            _, flt, pg = data.split(":")
            self._cf_zones(chat, mid, flt, int(pg))
        elif data == "addrec":
            self._addrec_zones(chat, mid)
        elif data.startswith("addrec:"):
            _, acc, zid = data.split(":")
            self._cf_add_start(chat, mid, int(acc), zid)
        elif data == "search":
            self.store.put_json(f"pend:{chat}", {"type": "gsearch"}, 600)
            self._show(chat, mid, SEARCH_PROMPT_TEXT,
                       [[btn("❌ انصراف", "menu")]])
        elif data == "favs":
            self._favs_page(chat, mid)
        elif data.startswith("favrm:"):
            self._fav_rm(chat, mid, data[6:])
        elif data == "accounts":
            self._cf_accounts(chat, mid)
        elif data == "accadd":
            self.store.put_json(f"pend:{chat}", {"type": "cf_acc_name"}, 600)
            self._show(chat, mid, "👤 چه اسمی برای اکانت کلادفلر بزارم؟",
                       [[btn("🔙 بازگشت", "accounts"),
                         btn("🏠 خانه", "menu")]])
        elif data == "accdel":
            self._cf_acc_del_list(chat, mid)
        elif data.startswith("accd:"):
            self._cf_acc_del_ask(chat, mid, data[5:])
        elif data.startswith("accdy:"):
            self._cf_acc_del_do(chat, mid, data[6:])
        elif data == "zsearch":
            sess = self._cf_sess(chat)
            if not sess.get("zid"):
                self._show(chat, mid, "⏳ نشست منقضی شد؛ دامنه را دوباره باز کن.",
                           home_kb())
            else:
                self.store.put_json(f"pend:{chat}", {"type": "zsearch"}, 600)
                self._show(chat, mid, "🔍 بخشی از نام یا مقدار رکورد را بفرستید:",
                           [[btn("❌ انصراف", "zones")]])
        elif data in ("noop", "ln", "arvan", "hf", "nd", "pghook", "um",
                      "srv", "dommon", "rem", "fmail", "lb", "settings",
                      "zset", "selmode", "zmail", "ztraf", "cref", "rtraf"):
            if data != "noop":
                self._soon(cb.get("id", ""))
        elif data.startswith("cfz:"):
            _, acc, zid = data.split(":")
            self._cf_records(chat, mid, int(acc), zid, 0)
        elif data.startswith("cfp:"):
            pg = int(data.split(":")[1])
            sess = self._cf_sess(chat)
            if not sess.get("zid"):
                self._show(chat, mid, "⏳ نشست منقضی شد؛ دامنه را دوباره باز کن.",
                           home_kb())
            else:
                self._cf_records(chat, mid, sess["acc"], sess["zid"], pg)
        elif data.startswith("cfr:"):
            rid = data[4:]
            acc, zid = self._cf_ctx(chat, rid)
            if not zid:
                self._show(chat, mid, "⏳ نشست منقضی شد؛ دامنه را دوباره باز کن.",
                           home_kb())
            else:
                self._cf_record(chat, mid, acc, zid, rid)
        elif data.startswith("cfdel:"):
            rid = data[6:]
            acc, zid = self._cf_ctx(chat, rid)
            if not zid:
                self._show(chat, mid, "⏳ نشست منقضی شد؛ دامنه را دوباره باز کن.",
                           home_kb())
            else:
                self._cf_del_ask(chat, mid, acc, zid, rid)
        elif data.startswith("cfdelok:"):
            rid = data[8:]
            acc, zid = self._cf_ctx(chat, rid)
            if not zid:
                self._show(chat, mid, "⏳ نشست منقضی شد؛ دامنه را دوباره باز کن.",
                           home_kb())
            else:
                self._cf_del_do(chat, mid, acc, zid, rid)
        elif data.startswith("cfpx:"):
            rid = data[5:]
            acc, zid = self._cf_ctx(chat, rid)
            if zid:
                self._cf_toggle_px(chat, mid, acc, zid, rid)
        elif data.startswith("cfttl:"):
            rid = data[6:]
            acc, zid = self._cf_ctx(chat, rid)
            if zid:
                self._cf_cycle_ttl(chat, mid, acc, zid, rid)
        elif data.startswith("cfedit:"):
            rid = data[7:]
            acc, zid = self._cf_ctx(chat, rid)
            if not zid:
                self._show(chat, mid, "⏳ نشست منقضی شد؛ دامنه را دوباره باز کن.",
                           home_kb())
                return
            self.store.put_json(f"pend:{chat}",
                                {"type": "cfedit", "acc": acc, "zid": zid,
                                 "rid": rid}, 900)
            self._show(chat, mid, "✏️ مقدار جدید را بفرستید:",
                       [[btn("❌ انصراف", f"cfr:{rid}")]])
        elif data.startswith("cfadd:"):
            _, acc, zid = data.split(":")
            self._cf_add_start(chat, mid, int(acc), zid)
        elif data.startswith("ct:"):
            rid = data[3:]
            acc, zid = self._cf_ctx(chat, rid)
            if zid:
                self._cf_type_toggle(chat, mid, acc, zid, rid)
        elif data.startswith("favtg:"):
            rid = data[6:]
            acc, zid = self._cf_ctx(chat, rid)
            if zid:
                self._fav_toggle(chat, mid, acc, zid, rid)
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
            [btn("➕ افزودن رکورد", "addrec"),
             btn("☁️ کلودفلر", "zones"),
             btn("🔍 جست و جو", "search")],
            [btn("🟢 لینود", "ln"),
             btn("🇩🇪 هتزنر", "hz"),
             btn("🇮🇷 آروان", "arvan")],
            [btn("🎛 پنل پاسارگارد", "pndef")],
            [btn("🧭 تعویض خودکار ساب فیلتر", "hf"),
             btn("📡 مانیتور نود پاسارگارد", "nd")],
            [btn("🔔 هشدار انقضا و حجم", "pghook"),
             btn("🔥 گزارش بد مصرف پاسارگارد", "um")],
            [btn("🖥 سرورها و مانیتورینگ", "srv"),
             btn("🌐 مانیتور دامنه و ترافیک‌ها", "dommon")],
            [btn("⏰ یادآورها", "rem"),
             btn("✉️ ایمیل دامنه", "fmail"),
             btn("⚖️ لود بالانسر IP", "lb")],
            [btn("⚙️ تنظیمات و راهنما و ...", "settings")],
        ]
        self._show(chat, mid,
                   f"به ربات نگهبان ابری خوش اومدی ⚙️ نسخه v{VERSION}",
                   kb)

    def _soon(self, cb_id: str) -> None:
        self.bot.answer(cb_id, "⏳ در نسخه سروری به‌زودی")

    # ---------- Cloudflare ----------

    ZONE_PAGE = 20

    def _cf_all_zones(self):
        out = []
        for i, a in enumerate(self._cf_accs()):
            try:
                for z in Cf(a["token"]).zones() or []:
                    z["_acc"] = i
                    out.append(z)
            except CfError:
                continue
        return out

    def _cf_zones(self, chat: int, mid: int = 0, flt: str = "all",
                  pg: int = 0) -> None:
        accs = self._cf_accs()
        if not accs:
            self._show(chat, mid, "❌ اکانت کلادفلر تنظیم نشده.", home_kb())
            return
        allz = self._cf_all_zones()
        if not allz:
            self._show(chat, mid,
                       "☁️ کلودفلر\n\n📭 هیچ دامنه‌ای یافت نشد.", home_kb())
            return
        zones = [z for z in allz
                 if z.get("status") == "active" and
                 (flt == "all" or z["_acc"] == int(flt))]
        pages = max(1, (len(zones) + self.ZONE_PAGE - 1) // self.ZONE_PAGE)
        pg = max(0, min(pg, pages - 1))
        kb = []
        rows = []
        for z in zones[pg * self.ZONE_PAGE:(pg + 1) * self.ZONE_PAGE]:
            rows.append(btn(f'{"🟢" if z.get("status") == "active" else "⚪"} '
                            f'{z["name"]}', f'cfz:{z["_acc"]}:{z["id"]}'))
        kb.extend(grid2(rows))
        kb.append([btn("⭐ ساب‌های منتخب", "favs"),
                   btn("📁 ✅ همه اکانت‌ها" if flt == "all"
                       else "📁 همه اکانت‌ها", "zf:all:0")])
        if len(accs) > 1:
            kb.extend(grid3([
                btn(f'{"👤 ✅ " if flt == str(j) else "👤 "}{a["name"]}',
                    f"zf:{j}:0") for j, a in enumerate(accs)]))
        kb.append([btn("➕ افزودن رکورد", "addrec"),
                   btn("👤 اکانت‌ها", "accounts")])
        if pages > 1:
            nav = []
            nav.append(btn("⬅️", f"zf:{flt}:{pg - 1}") if pg > 0
                       else EMPTY_BTN)
            nav.append(btn("🔙 بازگشت", "menu"))
            nav.append(btn("➡️", f"zf:{flt}:{pg + 1}")
                       if pg < pages - 1 else EMPTY_BTN)
            kb.append(nav)
        else:
            kb.append([btn("🏠 خانه", "menu")])
        label = "همه اکانت‌ها" if flt == "all" else \
            (accs[int(flt)]["name"] if flt.isdigit()
             and int(flt) < len(accs) else "؟")
        title = f"☁️ کلودفلر — {label}"
        if pages > 1:
            title += f" — صفحه {pg + 1} از {pages}"
        self._show(chat, mid, title, kb)

    def _cf_sess(self, chat: int) -> dict:
        try:
            return self.store.get_json(f"cfctx:{chat}", {}) or {}
        except Exception:
            return {}

    def _cf_save_sess(self, chat: int, sess: dict) -> None:
        self.store.put_json(f"cfctx:{chat}", sess, 86400)

    def _cf_ctx(self, chat: int, rid: str):
        sess = self._cf_sess(chat)
        if sess.get("zid"):
            return sess.get("acc", 0), sess["zid"]
        res = (sess.get("results") or {}).get(rid) or {}
        if res.get("zid"):
            return res.get("acc", 0), res["zid"]
        return 0, ""

    def _cf_records(self, chat: int, mid: int, acc: int, zid: str,
                    pg: int) -> None:
        try:
            client = self._cf_client(acc)
            recs = client.records(zid) or []
            zname = next((z["name"] for z in client.zones()
                          if z["id"] == zid), zid)
        except (IndexError, CfError) as e:
            self._show(chat, mid, f"❌ خطای کلادفلر: {esc(e)}", home_kb())
            return
        self._cf_save_sess(chat, {"acc": acc, "zid": zid})
        if not recs:
            self._show(chat, mid, f"📭 رکوردی برای {zname} یافت نشد.",
                       [[btn("➕ افزودن", f"cfadd:{acc}:{zid}")],
                        [btn("⚙️ تنظیمات", "zset")],
                        [btn("🔙 بازگشت", "zones"),
                         btn("🏠 خانه", "menu")]])
            return
        pages = max(1, (len(recs) + PAGE - 1) // PAGE)
        pg = max(0, min(pg, pages - 1))
        labels = self._dup_labels(recs, zname)
        rows = []
        for r in recs[pg * PAGE:(pg + 1) * PAGE]:
            rows.append(btn(labels[r.get("id")], f'cfr:{r.get("id")}'))
        kb = grid3(rows)
        kb.append([btn("⚙️ تنظیمات", "zset"),
                   btn("🗂 گروهی", "selmode"),
                   btn("➕ افزودن", f"cfadd:{acc}:{zid}"),
                   btn("🔍 جستجو", "zsearch")])
        if recs:
            kb.append([btn("✉️ ایمیل", "zmail"),
                       btn("📊 ترافیک ساب‌ها", "ztraf")])
        nav = [btn("◀️", f"cfp:{pg - 1}") if pg > 0 else EMPTY_BTN]
        if pages > 1:
            nav.append(btn(f"📄 {pg + 1}/{pages}", "noop"))
        else:
            nav.append(btn("🔙 بازگشت", "zones"))
        nav.append(btn("▶️", f"cfp:{pg + 1}") if pg < pages - 1
                   else EMPTY_BTN)
        kb.append(nav)
        if pages > 1:
            kb.append([btn("🔙 بازگشت", "zones"),
                       btn("🏠 خانه", "menu")])
        title = f"📋 {zname}"
        if pages > 1:
            title += f" — صفحه {pg + 1} از {pages}"
        self._show(chat, mid, title, kb)

    def _cf_record(self, chat: int, mid: int, acc: int, zid: str,
                   rid: str) -> None:
        try:
            client = self._cf_client(acc)
            recs = client.records(zid) or []
            zname = next((z["name"] for z in client.zones()
                          if z["id"] == zid), zid)
        except (IndexError, CfError) as e:
            self._show(chat, mid, f"❌ خطای کلادفلر: {esc(e)}", home_kb())
            return
        r = next((x for x in recs if x.get("id") == rid), None)
        if not r:
            self._show(chat, mid,
                       "❌ رکورد پیدا نشد (شاید حذف شده). از لیست دوباره بازش کن.")
            return
        faved = any(f.get("rid") == rid for f in self._favs())
        text = (f"✏️ ویرایش رکورد\n\n"
                f"📛 نام: <code>{esc(r.get('name'))}</code>\n"
                f"💡 مقدار: <code>{esc(r.get('content'))}</code>\n"
                f"📁 دامنه: <code>{esc(zname)}</code>")
        tbtn = (btn(f'🏷 نوع: {r.get("type")}', f"ct:{rid}")
                if r.get("type") in ("A", "CNAME")
                else btn("🔄 تغییر نوع", f"ct:{rid}"))
        kb = [
            [tbtn, btn("✏️ تغییر مقدار", f"cfedit:{rid}")],
            [btn(f"⏱ TTL: {self._ttl_label(r.get('ttl'))}", f"cfttl:{rid}"),
             btn(f'🌐 Proxy: {"روشن" if r.get("proxied") else "خاموش"}',
                 f"cfpx:{rid}")],
            [btn("🔗 CNAME به این", "cref"),
             btn("⭐ حذف از منتخب" if faved else "⭐ افزودن به منتخب",
                 f"favtg:{rid}")],
            [btn("🗑 حذف", f"cfdel:{rid}"),
             btn("📊 مشاهده ترافیک", "rtraf")],
            [btn("🔙 بازگشت", "zones"),
             btn("🏠 خانه", "menu")],
        ]
        self._show(chat, mid, text, kb)

    def _cf_del_ask(self, chat: int, mid: int, acc: int, zid: str,
                    rid: str) -> None:
        self._show(chat, mid, "⚠️ مطمئنی حذف شود؟",
                   [[btn("✅ بله، حذف کن", f"cfdelok:{rid}"),
                     btn("❌ انصراف", f"cfr:{rid}")]])

    def _cf_del_do(self, chat: int, mid: int, acc: int, zid: str,
                   rid: str) -> None:
        try:
            self._cf_client(acc).del_record(zid, rid)
            self._cf_records(chat, mid, acc, zid, 0)
        except (IndexError, CfError) as e:
            self._show(chat, mid, f"❌ حذف نشد: {esc(e)}", home_kb())

    def _cf_find(self, acc: int, zid: str, rid: str):
        try:
            recs = self._cf_client(acc).records(zid) or []
        except (IndexError, CfError):
            return None
        return next((x for x in recs if x.get("id") == rid), None)

    def _cf_toggle_px(self, chat: int, mid: int, acc: int, zid: str,
                      rid: str) -> None:
        r = self._cf_find(acc, zid, rid)
        if not r:
            self._show(chat, mid, "❌ رکورد پیدا نشد.", home_kb())
            return
        try:
            self._cf_client(acc).update_record(zid, r,
                                               proxied=not r.get("proxied"))
        except (IndexError, CfError) as e:
            self._show(chat, mid, f"❌ نشد: {esc(e)}", home_kb())
            return
        self._cf_record(chat, mid, acc, zid, rid)

    def _cf_cycle_ttl(self, chat: int, mid: int, acc: int, zid: str,
                      rid: str) -> None:
        r = self._cf_find(acc, zid, rid)
        if not r:
            self._show(chat, mid, "❌ رکورد پیدا نشد.", home_kb())
            return
        steps = [1, 120, 300, 1800, 3600, 21600, 86400]
        try:
            cur = int(r.get("ttl", 1))
        except (ValueError, TypeError):
            cur = 1
        nxt = steps[0] if cur not in steps \
            else steps[(steps.index(cur) + 1) % len(steps)]
        try:
            self._cf_client(acc).update_record(zid, r, ttl=nxt)
        except (IndexError, CfError) as e:
            self._show(chat, mid, f"❌ نشد: {esc(e)}", home_kb())
            return
        self._cf_record(chat, mid, acc, zid, rid)

    def _cf_type_toggle(self, chat: int, mid: int, acc: int, zid: str,
                        rid: str) -> None:
        r = self._cf_find(acc, zid, rid)
        if not r:
            self._show(chat, mid, "❌ رکورد پیدا نشد.", home_kb())
            return
        t = str(r.get("type", "")).upper()
        target = "CNAME" if t == "A" else "A" if t == "CNAME" else None
        if not target:
            self._show(chat, mid, "❌ فقط A↔CNAME.", home_kb())
            return
        v = str(r.get("content", "")).strip()
        import ipaddress

        def _ip(vv):
            try:
                ipaddress.ip_address(vv)
                return True
            except ValueError:
                return False
        if target == "A" and not _ip(v):
            self._show(chat, mid,
                       "❌ برای نوع A باید مقدار آیپی باشد.", home_kb())
            return
        if target == "CNAME" and _ip(v):
            self._show(chat, mid,
                       "❌ برای نوع CNAME باید مقدار هاست‌نیم باشد.",
                       home_kb())
            return
        try:
            self._cf_client(acc).update_record(zid, r, type=target)
        except (IndexError, CfError) as e:
            self._show(chat, mid, f"❌ نشد: {esc(e)}", home_kb())
            return
        self._cf_record(chat, mid, acc, zid, rid)

    def _fav_toggle(self, chat: int, mid: int, acc: int, zid: str,
                    rid: str) -> None:
        favs = self._favs()
        if any(f.get("rid") == rid for f in favs):
            favs = [f for f in favs if f.get("rid") != rid]
            msg = "⭐ از منتخب حذف شد."
        else:
            r = self._cf_find(acc, zid, rid)
            if not r:
                self._show(chat, mid, "❌ رکورد پیدا نشد.", home_kb())
                return
            try:
                zname = next((z["name"] for z in
                              self._cf_client(acc).zones()
                              if z["id"] == zid), zid)
            except (IndexError, CfError):
                zname = zid
            favs.append({"acc": acc, "zid": zid, "rid": rid,
                         "name": self._rec_label(str(r.get("name", "")),
                                                 r.get("type"), zname)})
            msg = "⭐ به منتخب اضافه شد."
        self.store.put_json("favs", favs)
        self.bot.send(chat, msg)
        self._cf_record(chat, 0, acc, zid, rid)

    def _favs_page(self, chat: int, mid: int = 0) -> None:
        favs = self._favs()
        if not favs:
            self._show(chat, mid, "⭐ ساب‌های منتخب\n\n📭 خالی است.",
                       [[btn("🔙 بازگشت", "zones"),
                         btn("🏠 خانه", "menu")]])
            return
        kb = []
        for f in favs[:40]:
            kb.append([btn(f.get("name", "?"), f'cfr:{f.get("rid")}')])
        kb.append([btn("🔙 بازگشت", "zones"),
                   btn("🏠 خانه", "menu")])
        self.store.put_json(f"cfctx:{chat}",
                            {"results": {f.get("rid"): {"acc": f.get("acc", 0),
                                                        "zid": f.get("zid")}
                                         for f in favs[:40]}}, 86400)
        self._show(chat, mid,
                   f"⭐ ساب‌های منتخب ({len(favs)}):", kb)

    def _fav_rm(self, chat: int, mid: int, rid: str) -> None:
        self.store.put_json("favs",
                            [f for f in self._favs()
                             if f.get("rid") != rid])
        self._favs_page(chat, mid)

    def _addrec_zones(self, chat: int, mid: int = 0) -> None:
        zones = [(i, z) for i, z in
                 enumerate(self._cf_all_zones())]
        zones = [(i, z) for i, z in zones if z.get("status") == "active"]
        if not zones:
            self._show(chat, mid, "📭 دامنه‌ای نیست.", home_kb())
            return
        kb = grid2([btn(z["name"], f"addrec:{i}:{z['id']}")
                    for i, z in zones[:40]])
        kb.append([btn("🔙 بازگشت", "zones"),
                   btn("🏠 خانه", "menu")])
        self._show(chat, mid, "➕ افزودن رکورد — دامنه را انتخاب کن:", kb)

    def _cf_add_start(self, chat: int, mid: int, acc: int,
                    zid: str) -> None:
        self.store.put_json(f"pend:{chat}",
                            {"type": "cfadd", "acc": acc, "zid": zid}, 900)
        self._show(chat, mid,
                   "➕ رکورد جدید — نوع و نام و مقدار را با فاصله بفرست:\n"
                   "<code>A sub 1.2.3.4</code>",
                   [[btn("❌ انصراف", "zones")]])

    def _cf_acc_of_zone(self, zid: str) -> int:
        for i, a in enumerate(self._cf_accs()):
            try:
                if any(z["id"] == zid for z in Cf(a["token"]).zones() or []):
                    return i
            except CfError:
                continue
        return 0

    def _cf_accounts(self, chat: int, mid: int = 0) -> None:
        accs = self._cf_accs()
        kb = []
        for i, a in enumerate(accs):
            kb.append([btn(f'👤 {a["name"]}', f"zf:{i}:0")])
        kb.append([btn("➕ افزودن اکانت", "accadd"),
                   btn("🗑 حذف اکانت", "accdel")])
        kb.append([btn("🔙 بازگشت", "zones"),
                   btn("🏠 خانه", "menu")])
        self._show(chat, mid, "👤 اکانت‌های کلادفلر:", kb)

    def _cf_acc_del_list(self, chat: int, mid: int = 0) -> None:
        accs = self._cf_accs()
        kb = [[btn(f'🗑 {a["name"]}', f"accd:{i}")]
              for i, a in enumerate(accs)]
        kb.append([btn("🔙 بازگشت", "accounts")])
        self._show(chat, mid, "🗑 کدام اکانت حذف شود؟", kb)

    def _cf_acc_del_ask(self, chat: int, mid: int, idx: str) -> None:
        try:
            name = self._cf_accs()[int(idx)]["name"]
        except (ValueError, IndexError):
            self._show(chat, mid, "❌ اکانت پیدا نشد.", home_kb())
            return
        self._show(chat, mid, f'⚠️ اکانت «{esc(name)}» حذف شود؟',
                   [[btn("✅ بله", f"accdy:{idx}"),
                     btn("❌ انصراف", "accdel")]])

    def _cf_acc_del_do(self, chat: int, mid: int, idx: str) -> None:
        accs = self._cf_accs()
        try:
            accs.pop(int(idx))
        except (ValueError, IndexError):
            self._show(chat, mid, "❌ اکانت پیدا نشد.", home_kb())
            return
        self.store.put_json("cf_accounts", accs)
        self.bot.send(chat, "✅ اکانت حذف شد.")
        self._cf_accounts(chat, 0)

    def _gsearch_go(self, chat: int, mid: int, q: str) -> None:
        q = q.lower()
        hits = []
        for i, a in enumerate(self._cf_accs()):
            try:
                client = Cf(a["token"])
                zones = [z for z in client.zones() or []
                         if z.get("status") == "active"]
            except CfError:
                continue
            for z in zones:
                if q in z["name"].lower():
                    hits.append((i, z, None))
                try:
                    recs = client.records(z["id"]) or []
                except CfError:
                    continue
                for r in recs:
                    if q in str(r.get("name", "")).lower() or \
                       q in str(r.get("content", "")).lower():
                        hits.append((i, z, r))
            if len(hits) >= 30:
                break
        if not hits:
            self._show(chat, mid, "🔍 چیزی پیدا نشد.", home_kb())
            return
        results = {}
        kb = []
        for i, z, r in hits[:30]:
            if r is None:
                kb.append([btn(f'📁 {z["name"]}', f'cfz:{i}:{z["id"]}')])
            else:
                results[r["id"]] = {"acc": i, "zid": z["id"]}
                kb.append([btn(self._rec_label(str(r.get("name", "")),
                                               r.get("type"), z["name"]),
                               f'cfr:{r["id"]}')])
        kb.append([btn("🔙 بازگشت", "zones"),
                   btn("🏠 خانه", "menu")])
        self._cf_save_sess(chat, {"results": results})
        self._show(chat, mid, f"🔍 نتایج ({len(hits)}):", kb)

    def _wizard_step(self, chat: int, mid: int, pend: dict, text: str) -> None:
        if pend.get("type") == "cfadd":
            parts = text.split()
            if len(parts) < 3:
                self.bot.send(chat, "❌ قالب درست نیست. مثال: <code>A sub 1.2.3.4</code>")
                return
            rtype, name, content = parts[0].upper(), parts[1], parts[2]
            try:
                self._cf_client(pend.get("acc", 0)).add_record(
                    pend["zid"], rtype, name, content)
            except (IndexError, CfError) as e:
                self.bot.send(chat, f"❌ ثبت نشد: {esc(e)}")
                return
            self.store.delete(f"pend:{chat}")
            self.bot.send(chat, "✅ رکورد ثبت شد.")
            self._cf_records(chat, 0, pend.get("acc", 0), pend["zid"], 0)
        elif pend.get("type") == "cfedit":
            r = self._cf_find(pend.get("acc", 0), pend["zid"], pend["rid"])
            if not r:
                self.store.delete(f"pend:{chat}")
                self.bot.send(chat, "❌ رکورد پیدا نشد.")
                return
            try:
                self._cf_client(pend.get("acc", 0)).update_record(
                    pend["zid"], r, content=text.strip())
            except (IndexError, CfError) as e:
                self.bot.send(chat, f"❌ نشد: {esc(e)}")
                return
            self.store.delete(f"pend:{chat}")
            self.bot.send(chat, "✅ مقدار عوض شد.")
            self._cf_record(chat, 0, pend.get("acc", 0), pend["zid"],
                            pend["rid"])
        elif pend.get("type") == "cf_acc_name":
            name = text.replace(" ", "")
            if not name:
                self.bot.send(chat, "❌ نام معتبر نیست.")
                return
            self.store.put_json(f"pend:{chat}",
                                {"type": "cf_acc_token", "name": name}, 600)
            self.bot.send(chat, "🔑 توکن API کلادفلر را بفرستید:")
        elif pend.get("type") == "cf_acc_token":
            tok = text.strip()
            try:
                Cf(tok).zones()
            except CfError as e:
                self.bot.send(chat, f"❌ توکن نامعتبر است: {esc(e)}")
                return
            accs = self._cf_accs()
            accs.append({"name": pend.get("name", "cf"), "token": tok})
            self.store.put_json("cf_accounts", accs)
            self.store.delete(f"pend:{chat}")
            self.bot.send(chat, "✅ اکانت اضافه شد.")
            self._cf_accounts(chat, 0)
        elif pend.get("type") == "gsearch":
            self.store.delete(f"pend:{chat}")
            self._gsearch_go(chat, 0, text.strip())
        elif pend.get("type") == "zsearch":
            sess = self._cf_sess(chat)
            self.store.delete(f"pend:{chat}")
            if not sess.get("zid"):
                self.bot.send(chat, "⏳ نشست منقضی شد.")
                return
            try:
                recs = self._cf_client(sess.get("acc", 0)).records(
                    sess["zid"]) or []
                zname = sess["zid"]
            except (IndexError, CfError) as e:
                self.bot.send(chat, f"❌ {esc(e)}")
                return
            q = text.strip().lower()
            hits = [r for r in recs
                    if q in str(r.get("name", "")).lower()
                    or q in str(r.get("content", "")).lower()][:30]
            if not hits:
                self.bot.send(chat, "🔍 چیزی پیدا نشد.")
                return
            labels = self._dup_labels(hits, zname)
            kb = grid3([btn(labels[r.get("id")], f'cfr:{r.get("id")}')
                        for r in hits])
            kb.append([btn("🔙 بازگشت", "zones"),
                       btn("🏠 خانه", "menu")])
            self.bot.send(chat, f"🔍 نتایج ({len(hits)}):", kb)
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
        elif pend.get("type") == "pg_add_url":
            url = text.strip().rstrip("/")
            if not (url.startswith("http://") or url.startswith("https://")):
                self.bot.send(chat, "❌ آدرس باید با http شروع شود.")
                return
            self.store.put_json(f"pend:{chat}",
                                {"type": "pg_add_user", "url": url}, 600)
            self.bot.send(chat, "👤 یوزر ادمین پنل را بفرستید:")
        elif pend.get("type") == "pg_add_user":
            self.store.put_json(f"pend:{chat}",
                                {"type": "pg_add_pass", "url": pend["url"],
                                 "username": text.strip()}, 600)
            self.bot.send(chat, "🔑 پسورد ادمین پنل را بفرستید:")
        elif pend.get("type") == "pg_add_pass":
            try:
                Pg(pend["url"], pend["username"], text.strip()).login()
            except PgError as e:
                self.bot.send(chat, f"❌ ورود ناموفق: {esc(e)}")
                return
            panels = self._pg_panels()
            panels.append({"url": pend["url"], "username": pend["username"],
                           "password": text.strip(),
                           "name": pend["url"].split("//")[-1].split("/")[0]})
            self.store.put_json("panels", panels)
            self.store.delete(f"pend:{chat}")
            self.bot.send(chat, "✅ پنل اضافه شد.")
            self._pg_home(chat, 0)
        elif pend.get("type") == "pg_search":
            self.store.delete(f"pend:{chat}")
            self._pg_users(chat, 0, pend["panel"], 0, text.strip().lower())
        else:
            self.store.delete(f"pend:{chat}")

    # ---------- Passargad panels ----------

    def _pg_panels(self) -> list:
        panels = self.store.get_json("panels", [])
        return panels if isinstance(panels, list) else []

    def _pg_home(self, chat: int, mid: int = 0) -> None:
        panels = self._pg_panels()
        if not panels:
            self._show(chat, mid, "🎛 پنل پاسارگارد\n\nپنلی نیست؛ اضافه کن:",
                       [[btn("➕ افزودن پنل", "pga")],
                        [btn("🏠 خانه", "menu")]])
            return
        kb = [[btn(f'📊 {p.get("name", p.get("url"))}', f'pgl:{i}:0')]
              for i, p in enumerate(panels)]
        kb.append([btn("➕ افزودن پنل", "pga"),
                   btn("🗑 حذف پنل", "pgdel")])
        kb.append([btn("🏠 خانه", "menu")])
        self._show(chat, mid,
                   f"🎛 پنل پاسارگارد ({len(panels)} پنل):", kb)

    def _pg_del_list(self, chat: int, mid: int = 0) -> None:
        panels = self._pg_panels()
        kb = [[btn(f'🗑 {p.get("name", p.get("url"))}', f'pgd:{i}')]
              for i, p in enumerate(panels)]
        kb.append([btn("🔙 بازگشت", "pg")])
        self._show(chat, mid, "🗑 کدام پنل حذف شود؟", kb)

    def _pg_del_ask(self, chat: int, mid: int, idx: str) -> None:
        try:
            name = self._pg_panels()[int(idx)].get("name", "?")
        except (ValueError, IndexError):
            self._show(chat, mid, "❌ پنل پیدا نشد.", home_kb())
            return
        self._show(chat, mid, f'⚠️ پنل «{esc(name)}» حذف شود؟',
                   [[btn("✅ بله", f"pgdy:{idx}"),
                     btn("❌ انصراف", "pgdel")]])

    def _pg_del_do(self, chat: int, mid: int, idx: str) -> None:
        panels = self._pg_panels()
        try:
            panels.pop(int(idx))
        except (ValueError, IndexError):
            self._show(chat, mid, "❌ پنل پیدا نشد.", home_kb())
            return
        self.store.put_json("panels", panels)
        self.bot.send(chat, "✅ پنل حذف شد.")
        self._pg_home(chat, 0)

    def _pg_login(self, i: int):
        p = self._pg_panels()[i]
        return Pg(p["url"], p["username"], p["password"]), p

    def _pg_users(self, chat: int, mid: int, i: int, pg: int,
                  q: str) -> None:
        try:
            pgc, p = self._pg_login(i)
            users = pgc.users(pgc.login())
        except (IndexError, PgError) as e:
            self._show(chat, mid, f"❌ {esc(e)}", home_kb())
            return
        if q:
            users = [u for u in users if q in u["username"].lower()]
        per, pages = 30, max(1, (len(users) + 29) // 30)
        pg = max(0, min(pg, pages - 1))
        page = users[pg * per:(pg + 1) * per]
        self.store.put_json(f"s:{chat}",
                            {"panel": i,
                             "users": [u["username"] for u in page]}, 3600)
        kb = []
        for idx, u in enumerate(page):
            p95 = pct(u["used"], u["limit"])
            flag = ""
            if p95 is not None and p95 >= 95:
                flag = "🔴 "
            elif p95 is not None and p95 >= 80:
                flag = "🟡 "
            kb.append([btn(f'{flag}{u["username"]}', f"pgu:{idx}")])
        if pages > 1:
            kb.append([btn(f"{pg + 1}/{pages}", f"pgl:{i}:{(pg + 1) % pages}")])
        kb.append([btn("🔍 جستجو", "pgq"),
                   btn("🔙 بازگشت", "pg")])
        kb.append([btn("🏠 خانه", "menu")])
        self._show(chat, mid,
                   f'👥 کاربران «{esc(p.get("name"))}» '
                   f"({len(users)} نفر):", kb)

    def _pg_user(self, chat: int, mid: int, idx: int) -> None:
        sess = self.store.get_json(f"s:{chat}", {}) or {}
        try:
            uname = (sess.get("users") or [])[idx]
            pgc, p = self._pg_login(sess["panel"])
            d = pgc.user(pgc.login(), uname) or {}
        except (IndexError, KeyError, TypeError, PgError) as e:
            self._show(chat, mid, f"❌ {esc(e)}", home_kb())
            return
        u = d.get("username", uname)
        used = float(d.get("used_traffic") or 0)
        lim = float(d.get("data_limit") or 0)
        p95 = pct(used, lim)
        dl = days_left(d.get("expire", d.get("expire_date")))
        txt = (f"👤 <code>{esc(u)}</code>\n"
               f"وضعیت: {esc(d.get('status', '—'))}\n"
               f"مصرف: {gb(used)} / {gb(lim) if lim else '∞'} گیگ"
               f'{f" ({p95}٪)" if p95 is not None else ""}\n'
               f'انقضا: {f"{dl} روز" if dl is not None else "—"}')
        self._show(chat, mid, txt,
                   [[btn("🔙 بازگشت", f'pgl:{sess["panel"]}:0')],
                    [btn("🏠 خانه", "menu")]])

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
