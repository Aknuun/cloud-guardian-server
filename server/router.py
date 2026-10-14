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
from jalali import fmt_jalali, parse_jalali_datetime
from ln import Ln, LnError, list_text
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


def btn(text: str, data: str, style: str = "") -> dict:
    b = {"text": text, "callback_data": data}
    if style:
        b["style"] = style
    return b


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
        elif data in ("noop", "arvan", "hf", "nd", "pghook", "um",
                      "srv", "dommon", "fmail", "lb", "settings",
                      "zset", "selmode", "zmail", "ztraf", "cref", "rtraf"):
            if data != "noop":
                self._soon(cb.get("id", ""))
        elif data == "ln":
            self._ln_home(chat, mid)
        elif data == "lna":
            self.store.put_json(f"pend:{chat}", {"type": "ln_add_name"}, 600)
            self._show(chat, mid,
                       "👤 نام دلخواه اکانت لینود را بفرستید (مثلاً: اصلی):",
                       [[btn("🔙 بازگشت", "ln"),
                         btn("🏠 خانه", "menu")]])
        elif data == "lndel":
            self._ln_del_list(chat, mid)
        elif data.startswith("lnd:"):
            self._ln_del_ask(chat, mid, data[4:])
        elif data.startswith("lndy:"):
            self._ln_del_do(chat, mid, data[5:])
        elif data.startswith("lnm:"):
            accs = self._ln_accs()
            try:
                i = int(data[4:])
                if 0 <= i < len(accs):
                    self.store.put("ln_sel", str(i))
            except (ValueError, IndexError):
                pass
            self._ln_home(chat, mid)
        elif data == "lnaccs":
            self._ln_accs_menu(chat, mid)
        elif data.startswith("lnacc:"):
            self._ln_acc_settings(chat, mid, int(data[6:]))
        elif data.startswith("lns:"):
            _, i, pg = data.split(":")
            self._ln_servers(chat, mid, int(i), int(pg))
        elif data.startswith("lnsi:"):
            _, i, sid = data.split(":")
            self._ln_info(chat, mid, int(i), sid)
        elif data.startswith("lnsu:"):
            _, i, sid, step = data.split(":")
            self._ln_op(chat, mid, int(i), sid, step)
        elif data.startswith("lnok:"):
            _, i, sid, step = data.split(":")
            self._ln_do(chat, mid, int(i), sid, step)
        elif data == "rem":
            self._rem_home(chat, mid)
        elif data == "remtxt":
            self.store.put_json(f"pend:{chat}",
                                {"type": "rem_text", "target": None}, 600)
            self._show(chat, mid, "✍️ متن یادآور را بفرستید:",
                       [[btn("🔙 بازگشت", "rem"),
                         btn("🏠 خانه", "menu")]])
        elif data == "remnote":
            pend = self.store.get_json(f"pend:{chat}", {}) or {}
            target = pend.get("target") if pend.get("type") == "rem_text" \
                else None
            self.store.put_json(f"pend:{chat}",
                                {"type": "rem_when", "target": target,
                                 "text": ""}, 900)
            self._show(chat, mid, REM_WHEN_TEXT, _rem_when_kb())
        elif data == "remdel":
            self._rem_del_list(chat, mid)
        elif data.startswith("remdx:"):
            self._rem_del_ask(chat, mid, data[6:])
        elif data.startswith("remdy:"):
            self._rem_del_do(chat, mid, data[6:])
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
            [btn("➕ افزودن رکورد", "addrec", "plain"),
             btn("☁️ کلودفلر", "zones"),
             btn("🔍 جست و جو", "search", "plain")],
            [btn("🟢 لینود", "ln", "success"),
             btn("🇩🇪 هتزنر", "hz", "success"),
             btn("🇮🇷 آروان", "arvan", "success")],
            [btn("🎛 پنل پاسارگارد", "pndef", "primary")],
            [btn("🧭 تعویض خودکار ساب فیلتر", "hf"),
             btn("📡 مانیتور نود پاسارگارد", "nd")],
            [btn("🔔 هشدار انقضا و حجم", "pghook"),
             btn("🔥 گزارش بد مصرف پاسارگارد", "um")],
            [btn("🖥 سرورها و مانیتورینگ", "srv", "plain"),
             btn("🌐 مانیتور دامنه و ترافیک‌ها", "dommon", "plain")],
            [btn("⏰ یادآورها", "rem", "plain"),
             btn("✉️ ایمیل دامنه", "fmail", "plain"),
             btn("⚖️ لود بالانسر IP", "lb", "plain")],
            [btn("⚙️ تنظیمات و راهنما و ...", "settings", "success")],
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
        kb.append([btn("⭐ ساب‌های منتخب", "favs", "success"),
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
            [btn("🔗 CNAME به این", "cref", "primary"),
             btn("⭐ حذف از منتخب" if faved else "⭐ افزودن به منتخب",
                 f"favtg:{rid}", "primary")],
            [btn("🗑 حذف", f"cfdel:{rid}", "danger"),
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
        elif pend.get("type") == "ln_add_name":
            name = text.replace(" ", "")
            if not name:
                self.bot.send(chat, "❌ نام معتبر نیست.")
                return
            self.store.put_json(f"pend:{chat}",
                                {"type": "ln_add_token", "name": name}, 600)
            self.bot.send(chat, "🔑 توکن API لینود را بفرستید:")
        elif pend.get("type") == "ln_add_token":
            tok = text.strip()
            try:
                acc = Ln(tok).call("/account")
                if not acc.get("email") and not acc.get("id"):
                    raise LnError("invalid")
            except LnError as e:
                self.bot.send(chat, f"❌ توکن نامعتبر است: {esc(e)}")
                return
            accs = self._ln_accs()
            accs.append({"name": pend.get("name", "ln"), "token": tok})
            self.store.put_json("ln_accounts", accs)
            self.store.delete(f"pend:{chat}")
            self.bot.send(chat, "✅ اکانت لینود اضافه شد.")
            self._ln_home(chat, 0)
        elif pend.get("type") == "ln_srv_rename":
            try:
                Ln(self._ln_accs()[pend["acc"]]["token"]).call(
                    f'/linode/instances/{pend["sid"]}', "PUT",
                    {"label": text.strip()})
                self.bot.send(chat, "✅ نام سرور تغییر کرد.")
            except (LnError, IndexError, KeyError) as e:
                self.bot.send(chat, f"❌ نشد: {esc(e)}")
                return
            self.store.delete(f"pend:{chat}")
            self._ln_info(chat, 0, pend["acc"], pend["sid"])
        elif pend.get("type") == "rem_text":
            self.store.put_json(f"pend:{chat}",
                                {"type": "rem_when",
                                 "target": pend.get("target"),
                                 "text": text.strip()}, 900)
            self.bot.send(chat, REM_WHEN_TEXT,
                          _rem_when_kb())
        elif pend.get("type") == "rem_when":
            try:
                at = parse_jalali_datetime(text.strip())
            except ValueError:
                self.bot.send(chat,
                              "❌ قالب درست نیست. مثال: <code>1404/06/20 14:30</code>")
                return
            import time as _t
            if at <= int(_t.time() * 1000):
                self.bot.send(chat, "❌ زمان باید در آینده باشد.")
                return
            lst = self._rem_list()
            import random as _r
            rid = f"r{int(_t.time())}{_r.randint(10, 99)}"
            lst.append({"id": rid, "text": pend.get("text") or "یادآور",
                        "at": at, "target": pend.get("target")})
            self.store.put_json("reminders", lst)
            self.store.delete(f"pend:{chat}")
            self.bot.send(chat,
                          f"✅ یادآور ثبت شد.\n🕒 {fmt_jalali(at)}",
                          [[btn("⏰ یادآورها", "rem"),
                            btn("🔙 بازگشت", "menu")]])
        else:
            self.store.delete(f"pend:{chat}")

    # ---------- Linode ----------

    def _ln_accs(self) -> list:
        accs = self.store.get_json("ln_accounts", [])
        return accs if isinstance(accs, list) else []

    def _ln_sel(self) -> int:
        accs = self._ln_accs()
        try:
            i = int(self.store.get("ln_sel") or 0)
            if 0 <= i < len(accs):
                return i
        except (ValueError, TypeError):
            pass
        return 0

    def _ln_home(self, chat: int, mid: int = 0) -> None:
        accs = self._ln_accs()
        if not accs:
            self._show(chat, mid, "🟢 لینود\n\nاکانتی نیست؛ اضافه کن:",
                       [[btn("➕ افزودن اکانت", "lna")],
                        [btn("🏠 خانه", "menu")]])
            return
        sel = self._ln_sel()
        kb = []
        lines = ["🟢 لینود", ""]
        if len(accs) > 1:
            acc_btns = [btn(f'{"✅ " if i == sel else "👤 "}{a["name"]}',
                             f"lnm:{i}") for i, a in enumerate(accs)]
            kb.extend(grid2(acc_btns) if len(accs) == 2
                      else grid3(acc_btns) if len(accs) == 3
                      else grid4(acc_btns))
        lines.append(f'«{accs[sel]["name"]}» انتخاب شده:')
        lines.append("")
        try:
            servers = Ln(accs[sel]["token"]).all("/linode/instances")
        except LnError:
            servers = None
        if servers is None:
            lines.append("⚠️ لیست سرورها خوانده نشد.")
        elif not servers:
            lines.append("📭 سروری نیست.")
        else:
            for s in servers[:20]:
                kb.append([btn(list_text(s), f'lnsi:{sel}:{s["id"]}',
                               "plain")])
        multi = len(accs) > 1
        nm = accs[sel]["name"]
        kb.append([btn("⚙️ تنظیمات" + (f" ({nm})" if multi else ""),
                     f"lnacc:{sel}")])
        kb.append([btn("🔙 بازگشت", "menu"),
                   btn("👤 اکانت‌ها", "lnaccs"),
                   btn("🏠 خانه", "menu")])
        self._show(chat, mid, "\n".join(lines), kb)

    def _ln_acc_settings(self, chat: int, mid: int, i: int) -> None:
        try:
            acc = self._ln_accs()[i]
        except IndexError:
            self._show(chat, mid, "❌ اکانت پیدا نشد.", home_kb())
            return
        tok = acc.get("token", "")
        masked = (tok[:8] + "…" + tok[-4:]) if len(tok) > 14 else "—"
        self._show(chat, mid,
                   f'⚙️ تنظیمات کلاینت\n\n👤 نام: <code>{esc(acc.get("name"))}</code>\n'
                   f"🔑 توکن: <code>{esc(masked)}</code>",
                   [[btn("🗑 حذف اکانت", f"lnd:{i}")],
                    [btn("🔙 بازگشت", "ln"),
                     btn("🏠 خانه", "menu")]])

    def _ln_accs_menu(self, chat: int, mid: int = 0) -> None:
        self._show(chat, mid, "👤 اکانت‌های لینود:",
                   [[btn("➕ افزودن اکانت", "lna")]] +
                   ([[btn("🗑 حذف اکانت", "lndel")]] if self._ln_accs()
                    else []) +
                   [[btn("🔙 بازگشت", "ln"), btn("🏠 خانه", "menu")]])

    def _ln_del_list(self, chat: int, mid: int = 0) -> None:
        accs = self._ln_accs()
        kb = [[btn(f'🗑 {a["name"]}', f'lnd:{i}')]
              for i, a in enumerate(accs)]
        kb.append([btn("🔙 بازگشت", "ln")])
        self._show(chat, mid, "🗑 کدام اکانت حذف شود؟", kb)

    def _ln_del_ask(self, chat: int, mid: int, idx: str) -> None:
        try:
            name = self._ln_accs()[int(idx)]["name"]
        except (ValueError, IndexError):
            self._show(chat, mid, "❌ اکانت پیدا نشد.", home_kb())
            return
        self._show(chat, mid, f'⚠️ اکانت «{esc(name)}» حذف شود؟',
                   [[btn("✅ بله", f"lndy:{idx}"),
                     btn("❌ انصراف", "lndel")]])

    def _ln_del_do(self, chat: int, mid: int, idx: str) -> None:
        accs = self._ln_accs()
        try:
            accs.pop(int(idx))
        except (ValueError, IndexError):
            self._show(chat, mid, "❌ اکانت پیدا نشد.", home_kb())
            return
        self.store.put_json("ln_accounts", accs)
        self.bot.send(chat, "✅ اکانت حذف شد.")
        self._ln_home(chat, 0)

    def _ln_servers(self, chat: int, mid: int, i: int, pg: int) -> None:
        try:
            acc = self._ln_accs()[i]
            servers = Ln(acc["token"]).all("/linode/instances")
        except (IndexError, LnError) as e:
            self._show(chat, mid, f"❌ {esc(e)}", home_kb())
            return
        per, pages = 10, max(1, (len(servers) + 9) // 10)
        pg = max(0, min(pg, pages - 1))
        kb = [[btn(list_text(s), f'lnsi:{i}:{s["id"]}', "plain")]
              for s in servers[pg * per:(pg + 1) * per]]
        if pages > 1:
            kb.append([btn("◀️", f"lns:{i}:{pg - 1}") if pg > 0
                       else EMPTY_BTN,
                       btn(f"📄 {pg + 1}/{pages}", "noop"),
                       btn("▶️", f"lns:{i}:{pg + 1}") if pg < pages - 1
                       else EMPTY_BTN])
        kb.append([btn("🔙 بازگشت", "ln"), btn("🏠 خانه", "menu")])
        self._show(chat, mid,
                   f'🖥 سرورهای «{esc(acc["name"])}» ({len(servers)}):', kb)

    def _ln_info(self, chat: int, mid: int, i: int, sid: str) -> None:
        try:
            ln = Ln(self._ln_accs()[i]["token"])
            s = ln.call(f"/linode/instances/{sid}")
        except (IndexError, LnError) as e:
            self._show(chat, mid, f"❌ {esc(e)}", home_kb())
            return
        if not s.get("id"):
            self._show(chat, mid, "❌ سرور پیدا نشد.", home_kb())
            return
        specs = s.get("specs") or {}
        ip4 = (s.get("ipv4") or ["—"])[0]
        try:
            tr = ln.call(f"/linode/instances/{sid}/transfer")
            used = tr.get("used") if isinstance(tr, dict) else None
            quota = tr.get("quota") if isinstance(tr, dict) else None
            bill = tr.get("billable") if isinstance(tr, dict) else None
        except LnError:
            used = quota = bill = None
        text = (
            f'🚀 <code>{esc(s.get("label"))}</code> '
            f'[<code>{esc(s.get("status"))}</code>]\n\n'
            f"🔗 IPv4: <code>{esc(ip4)}</code>\n"
            f"🔗 IPv6: <code>{esc(s.get('ipv6') or '—')}</code>\n"
            f'🌍 منطقه: <code>{esc(s.get("region"))}</code>\n'
            f'⚙️ مشخصات: <code>{esc(specs.get("vcpus", "?"))} هسته / '
            f'{gb(specs.get("memory", 0) * 1024 * 1024)}GB رم / '
            f'{gb(specs.get("disk", 0) * 1024 * 1024)}GB دیسک</code>\n'
            f'🖼️ تصویر: <code>{esc(s.get("image") or "—")}</code>\n'
            f"📊 ترافیک (GB):\n"
            f' • مصرف: <code>{esc("—" if used is None else round(used / 1024**3, 2))}</code>\n'
            f' • سهمیه: <code>{esc("—" if quota is None else round(quota / 1024**3, 2))}</code>\n'
            f' • مازاد: <code>{esc("—" if bill is None else round(bill / 1024**3, 2))}</code>')
        kb = grid2([
            btn("⚡ روشن", f"lnsu:{i}:{sid}:on"),
            btn("🔌 خاموش", f"lnsu:{i}:{sid}:off"),
            btn("🔄 ریبوت", f"lnsu:{i}:{sid}:reboot"),
            btn("✏️ تغییر نام", f"lnsu:{i}:{sid}:rename"),
            btn("🗑 حذف سرور", f"lnsu:{i}:{sid}:del", "danger"),
        ])
        kb.append([btn("🔙 بازگشت", f"lns:{i}:0"),
                   btn("🏠 خانه", "menu")])
        self._show(chat, mid, text, kb)

    def _ln_op(self, chat: int, mid: int, i: int, sid: str, step: str) -> None:
        back = f"lnsi:{i}:{sid}"
        if step == "rename":
            self.store.put_json(f"pend:{chat}",
                                {"type": "ln_srv_rename", "acc": i,
                                 "sid": sid}, 600)
            self._show(chat, mid, "✏️ نام جدید سرور را بفرستید:",
                       [[btn("🔙 انصراف", back)]])
            return
        label = {"on": "روشن کردن", "off": "خاموش کردن",
                 "reboot": "ریبوت", "del": "حذف سرور"}.get(step)
        if not label:
            self.bot.answer("", "")
            self._show(chat, mid, "⏳ در نسخه سروری به‌زودی", home_kb())
            return
        self._show(chat, mid, f"⚠️ مطمئنید «{label}» انجام شود؟",
                   [[btn("✅ بله", f"lnok:{i}:{sid}:{step}"),
                     btn("❌ انصراف", back)]])

    def _ln_do(self, chat: int, mid: int, i: int, sid: str, step: str) -> None:
        back = [[btn("🔙 بازگشت", f"lnsi:{i}:{sid}"),
                 btn("🏠 خانه", "menu")]]
        try:
            ln = Ln(self._ln_accs()[i]["token"])
        except IndexError:
            self._show(chat, mid, "❌ اکانت پیدا نشد.", home_kb())
            return
        try:
            if step == "del":
                ln.call(f"/linode/instances/{sid}", "DELETE")
                self.bot.send(chat, "✅ سرور حذف شد.")
                self._ln_servers(chat, 0, i, 0)
            elif step in ("on", "off", "reboot"):
                ln.call(f"/linode/instances/{sid}/"
                        f'{"boot" if step == "on" else "shutdown" if step == "off" else "reboot"}',
                        "POST", {})
                self.bot.send(chat, "✅ دستور ارسال شد.")
                self._ln_info(chat, 0, i, sid)
            else:
                self._show(chat, mid, "❓ عملیات ناشناخته.", back)
        except LnError as e:
            self._show(chat, mid, f"❌ خطا: {esc(e)}", back)

    # ---------- Reminders ----------

    def _rem_list(self) -> list:
        lst = self.store.get_json("reminders", [])
        return lst if isinstance(lst, list) else []

    def _rem_home(self, chat: int, mid: int = 0) -> None:
        lst = self._rem_list()
        lines = ["⏰ یادآورها", ""]
        kb = []
        if not lst:
            lines.append("📭 یادآوری نیست.")
        for r in lst[:20]:
            kb.append([btn(f'🔔 {r.get("text", "—")[:28]}', "rem",
                           "plain")])
        kb.append([btn("➕ یادآور جدید", "remtxt")])
        if lst:
            kb.append([btn("🗑 حذف", "remdel")])
        kb.append([btn("🏠 خانه", "menu")])
        self._show(chat, mid, "\n".join(lines), kb)

    def _rem_del_list(self, chat: int, mid: int = 0) -> None:
        lst = self._rem_list()
        kb = [[btn(f'🗑 {r.get("text", "—")[:28]}', f'remdx:{r.get("id")}')]
              for r in lst[:20]]
        kb.append([btn("🔙 بازگشت", "rem")])
        self._show(chat, mid, "🗑 کدام حذف شود؟", kb)

    def _rem_del_ask(self, chat: int, mid: int, rid: str) -> None:
        self._show(chat, mid, "⚠️ حذف شود؟",
                   [[btn("✅ بله", f"remdy:{rid}"),
                     btn("❌ انصراف", "remdel")]])

    def _rem_del_do(self, chat: int, mid: int, rid: str) -> None:
        self.store.put_json("reminders",
                            [r for r in self._rem_list()
                             if str(r.get("id")) != str(rid)])
        self.bot.send(chat, "✅ حذف شد.")
        self._rem_home(chat, 0)

    def rem_due(self, now_ms: int) -> list:
        due = [r for r in self._rem_list()
               if r.get("at") and r["at"] <= now_ms]
        if due:
            ids = {str(r.get("id")) for r in due}
            self.store.put_json("reminders",
                                [r for r in self._rem_list()
                                 if str(r.get("id")) not in ids])
        return due

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
            hz = Hz(self._hz_accs()[i]["token"])
            s = (hz.call(f"/servers/{sid}") or {}).get("server") or {}
        except (IndexError, HzError) as e:
            self._show(chat, mid, f"❌ {esc(e)}", home_kb())
            return
        if not s:
            self._show(chat, mid, "❌ سرور پیدا نشد.", home_kb())
            return
        from hz import gb, age_days, euro_rate, price_fmt, snap_count
        ib, ob = gb(s.get("ingoing_traffic")), gb(s.get("outgoing_traffic"))
        total = round((ib + ob) * 1000) / 1000
        incl = gb(s.get("included_traffic"))
        used = round(ob / incl * 1000) / 10 if incl else None
        bill = round(max(total - incl, 0) * 1000) / 1000 if incl else 0
        euro = euro_rate()
        p = ((s.get("server_type") or {}).get("prices") or [{}])[0]
        hourly = price_fmt((p.get("price_hourly") or {}).get("gross"), euro)
        monthly = price_fmt((p.get("price_monthly") or {}).get("gross"), euro)
        try:
            imgs = hz.all("/images")
            snaps = snap_count(imgs, sid)
        except HzError:
            snaps = "—"
        st = s.get("server_type") or {}
        loc = (s.get("datacenter") or {}).get("location") or s.get("location") or {}
        locs = f'{loc.get("country", "—")}, {loc.get("city", "—")}' if loc else "—"
        ip4 = ((s.get("public_net") or {}).get("ipv4") or {}).get("ip", "—")
        ip6 = ((s.get("public_net") or {}).get("ipv6") or {}).get("ip", "—")
        img = s.get("image") or {}
        created = str(s.get("created") or "")[:10] or "—"
        text = (
            f'🚀 نام: <code>{esc(s.get("name"))}</code> '
            f'[<code>{esc(s.get("status"))}</code>]\n'
            f"🔗 IPv4: <code>{esc(ip4)}</code>\n"
            f"🔗 IPv6: <code>{esc(ip6)}</code>\n"
            f"🌍 کشور/شهر: <code>{esc(locs)}</code>\n"
            f'⚙️ CPU: <code>{esc(st.get("cores", "—"))} Core</code>\n'
            f'💾 RAM: <code>{esc(st.get("memory", "—"))} GB</code>\n'
            f'💿 Disk: <code>{esc(st.get("disk", "—"))} GB</code>\n'
            f"📸 اسنپ‌شات: <code>{esc(snaps)}</code>\n"
            f'🖼️ تصویر: <code>{esc(img.get("name") or img.get("description") or "—")}</code>\n'
            f"📊 ترافیک:\n"
            f" • ورودی: <code>{ib} GB</code>\n"
            f" • خروجی: <code>{ob} GB</code>\n"
            f" • کل: <code>{total} GB</code>\n"
            f" • سهمیه: <code>{incl} GB</code>\n"
            f' • مصرف: <code>{("—" if used is None else f"{used}%")} [Out/Included]</code>\n'
            f" • مازاد: <code>{bill} GB</code>\n"
            f"💰 قیمت:\n"
            f" • ساعتی: <code>{esc(hourly)}</code>\n"
            f" • ماهانه: <code>{esc(monthly)}</code>\n"
            f"📅 ساخته‌شده: <code>{esc(created)} [{age_days(s.get('created'))} روز پیش]</code>")
        kb = grid2([
            btn("⚡ روشن", f"hzsu:{i}:{sid}:on"),
            btn("🔌 خاموش", f"hzsu:{i}:{sid}:off"),
            btn("🔄 ریبوت", f"hzsu:{i}:{sid}:reboot"),
            btn("🔄 ریست", f"hzsu:{i}:{sid}:reset"),
            btn("🔓 ریست رمز", f"hzsu:{i}:{sid}:pass"),
            btn("✏️ تغییر نام", f"hzsu:{i}:{sid}:rename"),
            btn("🛠️ ریبیلد", f"hzsu:{i}:{sid}:rebuild"),
            btn("⬆️ ارتقا", f"hzsu:{i}:{sid}:upgrade"),
            btn("📷 ساخت اسنپ‌شات", f"hzsu:{i}:{sid}:snap"),
            btn("🗑 حذف اسنپ‌شات", f"hzsu:{i}:{sid}:dsnap"),
            btn("🔗 اختصاص IPv4", f"hzsu:{i}:{sid}:a4"),
            btn("🔗 اختصاص IPv6", f"hzsu:{i}:{sid}:a6"),
            btn("❌ حذف IPv4", f"hzsu:{i}:{sid}:u4"),
            btn("❌ حذف IPv6", f"hzsu:{i}:{sid}:u6"),
            btn("🔄 جایگزینی IPv4", f"hzsu:{i}:{sid}:r4"),
        ])
        kb.append([btn("🔄 بروزرسانی", f"hzsi:{i}:{sid}"),
                   btn("🗑 حذف سرور", f"hzsu:{i}:{sid}:del")])
        kb.append([btn("🔙 بازگشت", f"hzs:{i}:0"),
                   btn("🏠 خانه", "menu")])
        self._show(chat, mid, text, kb)

    def _hz_op(self, chat: int, mid: int, i: int, sid: str, step: str) -> None:
        try:
            hz = Hz(self._hz_accs()[i]["token"])
            s = (hz.call(f"/servers/{sid}") or {}).get("server") or {}
        except (IndexError, HzError) as e:
            self._show(chat, mid, f"❌ {esc(e)}", home_kb())
            return
        if not s:
            self._show(chat, mid, "❌ سرور پیدا نشد.", home_kb())
            return
        back = f"hzsi:{i}:{sid}"
        home = [btn("🔙 بازگشت", back), btn("🏠 خانه", "menu")]
        if step == "rename":
            self.store.put_json(f"pend:{chat}",
                                {"type": "hz_srv_rename", "acc": i,
                                 "sid": sid}, 600)
            self._show(chat, mid, "✏️ نام جدید سرور را بفرستید:",
                       [[btn("🔙 انصراف", back)]])
            return
        if step == "rebuild":
            try:
                imgs = hz.all("/images")
            except HzError as e:
                self._show(chat, mid, f"❌ {esc(e)}", home_kb())
                return
            arch = (s.get("image") or {}).get("architecture")
            avail = [im for im in imgs
                     if im.get("type") in ("system", "snapshot")
                     and (not arch or im.get("architecture") == arch)]
            avail.sort(key=lambda x: 0 if x.get("type") == "system" else 1)
            if not avail:
                self._show(chat, mid, "❌ تصویری پیدا نشد.", home_kb())
                return
            kb = grid2([btn(str(im.get("name") or im.get("description")
                                 or im.get("id")),
                             f"hzsv:{i}:{sid}:rebuild:{im.get('id')}")
                        for im in avail[:50]])
            kb.append(home)
            self._show(chat, mid,
                       "🛠️ ریبیلد\n⚠️ همهٔ داده‌های سرور پاک می‌شود.\n\n"
                       "تصویر جدید را انتخاب کنید:", kb)
            return
        if step == "upgrade":
            try:
                types = hz.all("/server_types")
            except HzError as e:
                self._show(chat, mid, f"❌ {esc(e)}", home_kb())
                return
            cur = s.get("server_type") or {}
            up = [t for t in types
                  if t.get("architecture") == cur.get("architecture")
                  and t.get("id") != cur.get("id")
                  and (t.get("memory", 0) >= cur.get("memory", 0)
                       or t.get("cores", 0) >= cur.get("cores", 0)
                       or t.get("disk", 0) >= cur.get("disk", 0))]
            if not up:
                self._show(chat, mid,
                           "❌ پلن ارتقای بالاتری موجود نیست.", home_kb())
                return
            up.sort(key=lambda t: (t.get("memory", 0), t.get("cores", 0),
                                   t.get("disk", 0)))
            kb = grid2([btn(str(t.get("name")),
                             f"hzsv:{i}:{sid}:upgrade:{t.get('id')}")
                        for t in up[:40]])
            kb.append(home)
            warn = "⚠️ برای ارتقا اول سرور را خاموش کن.\n" \
                if s.get("status") != "off" else ""
            self._show(chat, mid,
                       f"⬆️ ارتقا\n{warn}پلن جدید را انتخاب کنید:", kb)
            return
        if step in ("a4", "a6"):
            typ = "ipv4" if step == "a4" else "ipv6"
            slot = (s.get("public_net") or {}).get(typ)
            if slot:
                self._show(chat, mid,
                           f"⚠️ این سرور از قبل {typ} اختصاصی دارد؛ اول آن را حذف کن.",
                           home_kb())
                return
            try:
                ips = hz.all("/primary_ips")
            except HzError as e:
                self._show(chat, mid, f"❌ {esc(e)}", home_kb())
                return
            avail = [ip for ip in ips
                     if not ip.get("assignee_id") and ip.get("type") == typ]
            if not avail:
                self._show(chat, mid,
                           f"❌ آی‌پی {typ} خالی پیدا نشد.", home_kb())
                return
            kb = grid2([btn(f'{ip.get("name", "")} {ip.get("ip", "")}'.strip(),
                             f"hzsv:{i}:{sid}:{step}:{ip.get('id')}")
                        for ip in avail[:40]])
            kb.append(home)
            self._show(chat, mid,
                       f"🔗 آی‌پی {typ.upper()} برای اختصاص (سرور خاموش می‌شود):",
                       kb)
            return
        if step == "dsnap":
            try:
                imgs = hz.all("/images")
            except HzError as e:
                self._show(chat, mid, f"❌ {esc(e)}", home_kb())
                return
            snaps = [im for im in imgs if im.get("type") == "snapshot"
                     and ((im.get("created_from") or {}).get("id")) == int(sid)]
            if not snaps:
                self._show(chat, mid,
                           "❌ اسنپ‌شاتی برای این سرور پیدا نشد.", home_kb())
                return
            kb = grid2([btn(str(im.get("name") or im.get("description")
                                 or im.get("id")),
                             f"hzsv:{i}:{sid}:dsnap:{im.get('id')}")
                        for im in snaps[:40]])
            kb.append(home)
            self._show(chat, mid, "🗑 اسنپ‌شات برای حذف:", kb)
            return
        label = {"on": "روشن کردن", "off": "خاموش کردن", "reset": "ریست شدید",
                 "reboot": "ریبوت", "pass": "ریست رمز", "del": "حذف سرور",
                 "snap": "ساخت اسنپ‌شات", "u4": "حذف اختصاص IPv4",
                 "u6": "حذف اختصاص IPv6",
                 "r4": "جایگزینی IPv4 با یکی تازه (قدیمی حذف و سرور دوباره روشن می‌شود)"}.get(step)
        if not label:
            self._show(chat, mid, "❓ عملیات ناشناخته.", home_kb())
            return
        if step == "del":
            self._show(chat, mid, f'⚠️ مطمئنید «{label}» روی «{s.get("name")}» انجام شود؟',
                       [[btn("✅ بله", f"hzok:{i}:{sid}:{step}"),
                         btn("❌ انصراف", back)]])
            return
        self._show(chat, mid, f'⚠️ مطمئنید «{label}» روی «{s.get("name")}» انجام شود؟',
                   [[btn("✅ بله", f"hzok:{i}:{sid}:{step}"),
                     btn("❌ انصراف", back)]])

    def _hz_pick(self, chat: int, mid: int, i: int, sid: str, step: str,
                 arg: str) -> None:
        try:
            Hz(self._hz_accs()[i]["token"]).call(f"/servers/{sid}")
        except (IndexError, HzError) as e:
            self._show(chat, mid, f"❌ {esc(e)}", home_kb())
            return
        label = {"rebuild": "ریبیلد", "upgrade": "ارتقا",
                 "a4": "اختصاص IPv4", "a6": "اختصاص IPv6",
                 "dsnap": "حذف اسنپ‌شات"}.get(step, step)
        self._show(chat, mid, f'⚠️ مطمئنید «{label}» انجام شود؟',
                   [[btn("✅ بله", f"hzok:{i}:{sid}:{step}:{arg}"),
                     btn("❌ انصراف", f"hzsi:{i}:{sid}")]])

    def _hz_do(self, chat: int, mid: int, i: int, sid: str, step: str,
               arg: str = "") -> None:
        from hz import ensure_off, primary_ip_id
        import datetime
        try:
            hz = Hz(self._hz_accs()[i]["token"])
        except IndexError:
            self._show(chat, mid, "❌ اکانت پیدا نشد.", home_kb())
            return
        back = [[btn("🔙 بازگشت", f"hzsi:{i}:{sid}"),
                 btn("🏠 خانه", "menu")]]
        try:
            if step == "del":
                hz.call(f"/servers/{sid}", "DELETE")
                self.bot.send(chat, "✅ سرور حذف شد.")
                self._hz_servers(chat, 0, i, 0)
                return
            if step in ("on", "off", "reboot", "reset"):
                hz.call(f"/servers/{sid}/actions/power{step}"
                        if step in ("on", "off") else
                        f"/servers/{sid}/actions/{step}", "POST", {})
                self.bot.send(chat, "✅ دستور ارسال شد.")
                self._hz_info(chat, 0, i, sid)
                return
            if step == "pass":
                r = hz.call(f"/servers/{sid}/actions/reset_password",
                            "POST", {})
                pw = (r.get("action") or {}).get("root_password") or \
                    (r.get("root_password") or "")
                self.bot.send(chat,
                              "🔓 رمز root جدید:\n"
                              f"<code>{esc(pw)}</code>\n\n"
                              "⚠️ آن را امن نگه دار.")
                self._hz_info(chat, 0, i, sid)
                return
            if step == "snap":
                name = f"snap-{sid}-{datetime.datetime.now():%Y%m%d-%H%M}"
                hz.call(f"/servers/{sid}/actions/create_image", "POST",
                        {"description": name, "type": "snapshot"})
                self.bot.send(chat, f"✅ ساخت اسنپ‌شات شروع شد ({esc(name)}).")
                self._hz_info(chat, 0, i, sid)
                return
            if step == "rebuild":
                hz.call(f"/servers/{sid}/actions/rebuild", "POST",
                        {"image": arg})
                self.bot.send(chat, "✅ ریبیلد شروع شد.")
                self._hz_info(chat, 0, i, sid)
                return
            if step == "upgrade":
                s = (hz.call(f"/servers/{sid}") or {}).get("server") or {}
                if s.get("status") != "off":
                    self._show(chat, mid,
                               "⚠️ برای ارتقا اول سرور را خاموش کن.", back)
                    return
                hz.call(f"/servers/{sid}/actions/change_type", "POST",
                        {"server_type": arg, "upgrade_disk": True})
                self.bot.send(chat, "✅ ارتقا شروع شد.")
                self._hz_info(chat, 0, i, sid)
                return
            if step in ("a4", "a6"):
                if not ensure_off(hz, sid):
                    self._show(chat, mid,
                               "⚠️ سرور در ۳۰ ثانیه خاموش نشد؛ دوباره تلاش کن.",
                               back)
                    return
                hz.call(f"/primary_ips/{arg}/actions/assign", "POST",
                        {"assignee_id": int(sid),
                         "assignee_type": "server"})
                self.bot.send(chat, "✅ آی‌پی اختصاص یافت.")
                self._hz_info(chat, 0, i, sid)
                return
            if step in ("u4", "u6"):
                s = (hz.call(f"/servers/{sid}") or {}).get("server") or {}
                slot = (s.get("public_net") or {}).get(
                    "ipv4" if step == "u4" else "ipv6")
                ipid = primary_ip_id(slot)
                if not ipid:
                    self._show(chat, mid, "ℹ️ آی‌پی اختصاصی ندارد.", back)
                    return
                if not ensure_off(hz, sid):
                    self._show(chat, mid,
                               "⚠️ سرور در ۳۰ ثانیه خاموش نشد؛ دوباره تلاش کن.",
                               back)
                    return
                hz.call(f"/primary_ips/{ipid}/actions/unassign", "POST", {})
                self.bot.send(chat, "✅ اختصاص حذف شد.")
                self._hz_info(chat, 0, i, sid)
                return
            if step == "r4":
                s = (hz.call(f"/servers/{sid}") or {}).get("server") or {}
                dc = (s.get("datacenter") or {}).get("name")
                if not dc:
                    self._show(chat, mid,
                               "❌ دیتاسنتر سرور مشخص نیست.", back)
                    return
                old = primary_ip_id((s.get("public_net") or {}).get("ipv4"))
                if not ensure_off(hz, sid):
                    self._show(chat, mid,
                               "⚠️ سرور در ۳۰ ثانیه خاموش نشد؛ دوباره تلاش کن.",
                               back)
                    return
                c = hz.call("/primary_ips", "POST",
                            {"type": "ipv4",
                             "name": f"replace-{sid}",
                             "datacenter": dc, "auto_delete": True})
                nip = (c or {}).get("primary_ip") or {}
                if not nip.get("id"):
                    self._show(chat, mid, "❌ ساخت IPv4 تازه ناموفق شد.",
                               back)
                    return
                if old:
                    hz.call(f"/primary_ips/{old}/actions/unassign",
                            "POST", {})
                hz.call(f"/primary_ips/{nip['id']}/actions/assign", "POST",
                        {"assignee_id": int(sid), "assignee_type": "server"})
                if old:
                    try:
                        hz.call(f"/primary_ips/{old}", "DELETE")
                    except HzError:
                        pass
                try:
                    hz.call(f"/servers/{sid}/actions/poweron", "POST", {})
                except HzError:
                    pass
                self.bot.send(chat,
                              "✅ جایگزینی انجام شد.\n"
                              f'IPv4 تازه: <code>{esc(nip.get("ip"))}</code>')
                self._hz_info(chat, 0, i, sid)
                return
            if step == "dsnap":
                hz.call(f"/images/{arg}", "DELETE")
                self.bot.send(chat, "✅ اسنپ‌شات حذف شد.")
                self._hz_info(chat, 0, i, sid)
                return
            self._show(chat, mid, "❓ عملیات ناشناخته.", back)
        except HzError as e:
            self._show(chat, mid, f"❌ خطا: {esc(e)}", back)
REM_WHEN_TEXT = ("🕒 زمان یادآور:\nتاریخ شمسی و ساعت را بفرستید.\n"
                     "مثال: <code>1404/06/20 14:30</code>")


def _rem_when_kb() -> list:
    return [[btn("🔙 بازگشت", "rem"), btn("🏠 خانه", "menu")]]


class _LnHzRouterMixin:
    pass