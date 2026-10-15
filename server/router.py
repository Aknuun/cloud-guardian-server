"""Update router: menus, callbacks and wizards.

Conventions (kept tiny on purpose):
- callback data: <section>:<args...> (e.g. cfz:<zone_id>)
- per-chat UI session:  s:<chat_id>  (JSON, 1h TTL)
- pending text input:   pend:<chat_id> (JSON, 15min TTL)
- every handler returns (text, keyboard) or sends directly.
"""
from __future__ import annotations

import html
import urllib.parse

from cf import Cf, CfError
from arvan import Arvan, ArvanError, norm_key
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


_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def fa_d(n) -> str:
    return str(n).translate(_FA)


def btn(text: str, data: str, style: str = "") -> dict:
    b = {"text": text, "callback_data": data}
    if style:
        b["style"] = style
    return b


def home_kb() -> list:
    return [[btn("🏠 خانه", "menu")]]


def _pg_tg_username(bot_token: str) -> str:
    import json as _j
    import urllib.request as _u
    try:
        with _u.urlopen(_u.Request(
                f"https://api.telegram.org/bot{bot_token}/getMe"),
                timeout=20) as r:
            un = (_j.load(r).get("result") or {}).get("username") or ""
            return str(un)
    except Exception:
        return ""


def _value_hint(value: str, rtype: str):
    """None یعنی معتبر؛ وگرنه متن راهنمای فارسی. مثل کد قبلی."""
    import ipaddress as _ip
    import re as _re
    s = str(value or "").strip()
    if not s:
        return "مقدار خالی است؛ یک IPv4 یا نام دامنهٔ معتبر بفرستید."

    def _is_ip(x):
        try:
            _ip.ip_address(x)
            return True
        except ValueError:
            return False

    def _is_v4(x):
        try:
            return isinstance(_ip.ip_address(x), _ip.IPv4Address)
        except ValueError:
            return False

    def _host(x):
        return bool(_re.match(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$",
                              x, _re.I))
    if rtype == "CNAME":
        if _is_ip(s):
            return "برای رکورد CNAME باید نام دامنهٔ معتبر بفرستید (مثل sub.example.com)؛ آی‌پی خام پذیرفته نمی‌شود."
        if _host(s):
            return None
        return "برای رکورد CNAME باید نام دامنهٔ معتبر بفرستید (مثل sub.example.com)."
    if rtype == "A":
        if _is_v4(s):
            return None
        if _is_ip(s):
            return "برای رکورد A باید IPv4 معتبر بفرستید (مثل 1.2.3.4)؛ مقدار ارسالی IPv6 است."
        if _host(s):
            return "برای رکورد A باید IPv4 معتبر بفرستید (مثل 1.2.3.4)؛ نام دامنه پذیرفته نمی‌شود."
        return "برای رکورد A باید IPv4 معتبر بفرستید (مثل 1.2.3.4)."
    if rtype == "AAAA":
        try:
            if isinstance(_ip.ip_address(s), _ip.IPv6Address):
                return None
        except ValueError:
            pass
        return "برای رکورد AAAA باید IPv6 معتبر بفرستید."
    return None


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
        elif data.startswith("hzsc:"):
            i = int(data[5:])
            self.store.put_json(f"pend:{chat}",
                                {"type": "hz_srv_remark", "acc": i}, 600)
            self._show(chat, mid,
                       "➕ ساخت سرور هتزنر\n\nنام سرور را بفرستید (بدون فاصله، مثلاً web1):",
                       [[btn("🔙 انصراف", "hz")]])
        elif data.startswith("hzsl:"):
            _, i, loc = data.split(":")
            self._hz_pick_plan(chat, mid, int(i), loc)
        elif data.startswith("hzscp:"):
            _, i, plan = data.split(":")
            self._hz_pick_image(chat, mid, int(i), plan)
        elif data.startswith("hzsci:"):
            _, i, img = data.split(":")
            self._hz_create(chat, mid, int(i), img)
        elif data.startswith("hzsv:"):
            _, i, sid, step, arg = data.split(":")
            self._hz_pick(chat, mid, int(i), sid, step, arg)
        elif data.startswith("hzok:"):
            parts = data.split(":")
            i, sid, step = int(parts[1]), parts[2], parts[3]
            self._hz_do(chat, mid, i, sid, step,
                        parts[4] if len(parts) > 4 else "")
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
        elif data in ("noop",
                      "fmail",
                      "zset", "zmail", "ztraf", "cref", "rtraf",
                      "srvsub", "srvpw",
                      "srvstats", "srvupd", "srvedit"):
            if data != "noop":
                self._soon(cb.get("id", ""))
        elif data == "sslm":
            self._sslm_home(chat, mid)
        elif data == "ssdma":
            self.store.put_json(f"pend:{chat}", {"type": "ssl_host"}, 600)
            self._show(chat, mid,
                       "🔐 دامنه یا ساب‌دامنه‌ای که می‌خواهید گواهی‌اش نظارت شود را بفرستید (مثلاً example.com):",
                       [[btn("🔙 انصراف", "sslm")]])
        elif data == "sslmd":
            from sslmon import get_monitors
            mons = get_monitors(self.store)
            if not mons:
                self._show(chat, mid, "📭 دامنه‌ای ثبت نشده.",
                           [[btn("🔙 بازگشت", "sslm"),
                             btn("🏠 خانه", "menu")]])
            else:
                self._show(chat, mid, "🗑 کدام دامنه از نظارت حذف شود?",
                           [[btn(f'🗑 {m.get("host")}', f'sslmdy:{i}')]
                            for i, m in enumerate(mons)] +
                           [[btn("🔙 بازگشت", "sslm"),
                             btn("🏠 خانه", "menu")]])
        elif data.startswith("sslmdy:"):
            from sslmon import get_monitors
            mons = get_monitors(self.store)
            try:
                rm = mons.pop(int(data[7:]))
                self.store.put_json("ssl_monitor", mons)
                self.bot.send(chat, f'✅ «{rm.get("host")}» از نظارت حذف شد.')
            except (ValueError, IndexError):
                self._show(chat, mid, "❌ دامنه پیدا نشد.", home_kb())
                return
            self._sslm_home(chat, 0)
        elif data == "dommon":
            self._show(chat, mid, "🌐 مانیتور دامنه و ترافیک‌ها\n\nیک بخش را انتخاب کنید:",
                       [[btn("🔐 مانیتور SSL", "sslm"),
                         btn("🗓 مانیتور انقضای دامنه", "domexp")],
                        [btn("📊 ترافیک ساب‌ها", "traf"),
                         btn("☁️ سهمیهٔ کلادفلر", "quota")],
                        [btn("🏠 خانه", "menu")]])
        elif data == "quota":
            self._cf_quota(chat, mid)
        elif data == "srvimport":
            self._srv_import(chat, mid)
        elif data == "domexp":
            self._domexp_refresh(chat, mid)
        elif data == "domexpp" or data.startswith("domexpp:"):
            p = data.split(":")
            self._domexp_home(chat, mid, int(p[1]) if len(p) > 1
                              and p[1].lstrip("-").isdigit() else 0)
        elif data == "domexpall":
            self._domexp_add_all(chat, mid)
        elif data == "domexpa":
            self.store.put_json(f"pend:{chat}", {"type": "domexp_domain"}, 600)
            self._show(chat, mid,
                       "✍️ دامنه‌ای که می‌خواهید انقضایش نظارت شود را بفرستید (مثلاً example.ir یا example.com):\n\n"
                       "ℹ️ دامنهٔ .ir از whois.nic.ir استعلام می‌شود؛ اگر ایرنیک تاریخ را مخفی کرده باشد و «🔑 کلید API» ثبت شده باشد، خودکار از WhoisXMLAPI گرفته می‌شود؛ وگرنه از «🗓 ثبت تاریخ دستی» وارد کن.",
                       [[btn("🔙 انصراف", "domexpp:0")]])
        elif data == "domexpcal":
            self._domexp_cal_list(chat, mid)
        elif data.startswith("domexpi:"):
            self._domexp_cal_domain(chat, mid,
                                    urllib.parse.unquote(data[8:]))
        elif data == "domexpd":
            self._domexp_del_list(chat, mid)
        elif data.startswith("domexpdy:"):
            self._domexp_del(chat, mid, data[9:])
        elif data == "domexpkey":
            self._domexp_key_page(chat, mid)
        elif data == "domexpk":
            self.store.put_json(f"pend:{chat}", {"type": "domexp_key"}, 900)
            self._show(chat, mid,
                       "🔑 کلید API را بفرستید:\n(برای حذف کلید، «-» بفرستید)",
                       [[btn("🔙 انصراف", "domexpkey")]])
        elif data == "domexpkdel":
            self.store.put_json("dom_expiry_cfg",
                                {"provider": "whoisxmlapi_domaininfo",
                                 "key": ""})
            self._show(chat, mid, "🗑 کلید API حذف شد.",
                       [[btn("🔙 بازگشت", "domexpkey"),
                         btn("🏠 خانه", "menu")]])
        elif data == "domexptest":
            self._domexp_test(chat, mid)
        elif data.startswith("trazdely:"):
            p = data.split(":")
            self._traf_zero_do(chat, mid, int(p[1]), p[2])
        elif data == "traf":
            self._traf_zones(chat, mid, "all", 0)
        elif data.startswith("trazf:"):
            p = data.split(":")
            self._traf_zones(chat, mid, p[1], int(p[2]) if len(p) > 2
                             and p[2].lstrip("-").isdigit() else 0)
        elif data.startswith("traz:"):
            p = data.split(":")
            self._traf_zone(chat, mid, int(p[1]), p[2],
                            int(p[3]) if len(p) > 3 else 0)
        elif data.startswith("trazdel:"):
            p = data.split(":")
            self._traf_zero_confirm(chat, mid, int(p[1]), p[2])
        elif data == "srvaddbulk":
            self.store.put_json(f"pend:{chat}", {"type": "srv_bulk"}, 900)
            self._show(chat, mid,
                       "📥 هر خط یک سرور:\n<code>host user password [port]</code>",
                       [[btn("🔙 انصراف", "srv")]])
        elif data == "srv":
            self._srv_home(chat, mid)
        elif data == "srvadd":
            self.store.put_json(f"pend:{chat}", {"type": "srv_add_host"},
                                600)
            self._show(chat, mid, "🌐 هاست یا آی‌پی سرور را بفرستید:",
                       [[btn("🔙 انصراف", "srv")]])
        elif data == "srvdel":
            lst = self._srv_list()
            kb = [[btn(f'🗑 {s.get("name")}', f'srvdelx:{s.get("id")}')]
                  for s in lst]
            kb.append([btn("🔙 بازگشت", "srv")])
            self._show(chat, mid, "🗑 کدام سرور حذف شود؟", kb)
        elif data.startswith("srvopen:"):
            self._srv_open(chat, mid, data[8:])
        elif data.startswith("srvdelx:"):
            self._srv_del_ask(chat, mid, data[8:])
        elif data.startswith("srvdely:"):
            self._srv_del_do(chat, mid, data[8:])
        elif data.startswith("srvinfo:"):
            self._srv_info(chat, mid, data[8:])
        elif data.startswith("srvdisk:"):
            self._srv_disk(chat, mid, data[8:])
        elif data.startswith("srvreboot:"):
            self._show(chat, mid, "⚠️ سرور ریبوت شود؟",
                       [[btn("✅ بله", f"srvrebootgo:{data[9:]}"),
                         btn("❌ انصراف", f"srvopen:{data[9:]}")]])
        elif data.startswith("srvrebootgo:"):
            self._srv_reboot(chat, mid, data[12:])
        elif data == "srvmon":
            self._srvmon_home(chat, mid)
        elif data == "srvmontg":
            from monitors import srvmon_cfg, srvmon_save
            cfg = srvmon_cfg(self.store)
            cfg["enabled"] = not cfg.get("enabled", True)
            srvmon_save(self.store, cfg)
            self._srvmon_home(chat, mid)
        elif data.startswith("srvmonh:"):
            self.store.put_json(f"pend:{chat}",
                                {"type": "srvmon_num",
                                 "key": data[8:]}, 600)
            self._show(chat, mid, "🔢 عدد جدید را بفرستید:",
                       [[btn("🔙 انصراف", "srvmon")]])
        elif data == "srvmonrun":
            self._show(chat, mid, "⏳ در حال بررسی همه سرورها…",
                       home_kb())
            self._srvmon_run(chat)
        elif data.startswith("srvstats:"):
            self._srvstats(chat, mid, data[9:])
        elif data == "hf":
            self._hf_home(chat, mid)
        elif data == "hftg":
            from hostfilter import get_cfg, save_cfg
            cfg = get_cfg(self.store)
            cfg["enabled"] = not cfg.get("enabled", True)
            save_cfg(self.store, cfg)
            self._hf_home(chat, mid)
        elif data == "hfforceall":
            self._show(chat, mid,
                       "🔄 بررسی کامل شروع شد؛ نتیجه همین‌جا اعلام می‌شود.",
                       home_kb())
            self._hf_run_bg(chat, False, "")
        elif data == "hflist":
            self._hf_hosts(chat, mid)
        elif data.startswith("hfexc:"):
            _, pid, hid, field = data.split(":")
            self._hf_exc_toggle(chat, mid, pid, hid, field)
        elif data == "hfexcall":
            from hostfilter import get_cfg, save_cfg
            cfg = get_cfg(self.store)
            ex = set(cfg.get("exceptions") or [])
            for key in self._hf_all_keys():
                ex.add(key)
            cfg["exceptions"] = sorted(ex)
            save_cfg(self.store, cfg)
            self._hf_hosts(chat, mid)
        elif data == "hfunexcall":
            from hostfilter import get_cfg, save_cfg
            cfg = get_cfg(self.store)
            cfg["exceptions"] = []
            save_cfg(self.store, cfg)
            self._hf_hosts(chat, mid)
        elif data.startswith("hfexcpanel:"):
            from hostfilter import get_cfg, save_cfg
            cfg = get_cfg(self.store)
            ex = set(cfg.get("exceptions") or [])
            for key in self._hf_all_keys(pid=int(data[11:])):
                ex.add(key)
            cfg["exceptions"] = sorted(ex)
            save_cfg(self.store, cfg)
            self._hf_hosts(chat, mid)
        elif data.startswith("hfunexcpanel:"):
            from hostfilter import get_cfg, save_cfg
            cfg = get_cfg(self.store)
            pre = str(int(data[13:])) + ":"
            cfg["exceptions"] = [x for x in (cfg.get("exceptions") or [])
                                 if not str(x).startswith(pre)]
            save_cfg(self.store, cfg)
            self._hf_hosts(chat, mid)
        elif data == "hfgmode":
            self.store.put_json(f"hfsel:{chat}", {"on": True, "sel": []}, 1800)
            self._hf_hosts(chat, mid)
        elif data == "hfgdone":
            self.store.delete(f"hfsel:{chat}")
            self._hf_hosts(chat, mid)
        elif data == "hfgall":
            sel = self.store.get_json(f"hfsel:{chat}", {}) or {}
            sel["sel"] = self._hf_all_keys()
            sel["on"] = True
            self.store.put_json(f"hfsel:{chat}", sel, 1800)
            self._hf_hosts(chat, mid)
        elif data == "hfgclr":
            self.store.put_json(f"hfsel:{chat}", {"on": True, "sel": []}, 1800)
            self._hf_hosts(chat, mid)
        elif data.startswith("hfgs:"):
            _, pid, hid = data.split(":")
            sel = self.store.get_json(f"hfsel:{chat}", {}) or {}
            cur = set(sel.get("sel") or [])
            key = pid + ":" + hid
            if key in cur:
                cur.discard(key)
            else:
                cur.add(key)
            sel.update(on=True, sel=sorted(cur))
            self.store.put_json(f"hfsel:{chat}", sel, 1800)
            self._hf_hosts(chat, mid)
        elif data == "hfgexc":
            from hostfilter import get_cfg, save_cfg
            cfg = get_cfg(self.store)
            ex = set(cfg.get("exceptions") or [])
            sel = (self.store.get_json(f"hfsel:{chat}", {}) or {}).get("sel") or []
            for key in sel:
                ex.add(key)
            cfg["exceptions"] = sorted(ex)
            save_cfg(self.store, cfg)
            self.store.delete(f"hfsel:{chat}")
            self._hf_hosts(chat, mid)
        elif data == "hfgunexc":
            from hostfilter import get_cfg, save_cfg
            cfg = get_cfg(self.store)
            sel = set((self.store.get_json(f"hfsel:{chat}", {}) or {}).get("sel") or [])
            cfg["exceptions"] = [x for x in (cfg.get("exceptions") or [])
                                 if x not in sel and not any(x == k or x.startswith(k + ":") for k in sel)]
            save_cfg(self.store, cfg)
            self.store.delete(f"hfsel:{chat}")
            self._hf_hosts(chat, mid)
        elif data == "hfhist":
            self._hf_hist(chat, mid)
        elif data == "hfsetch":
            self._hf_settings(chat, mid)
        elif data.startswith("hfnum:"):
            self.store.put_json(f"pend:{chat}",
                                {"type": "hf_num",
                                 "key": data[6:]}, 600)
            self._show(chat, mid, "🔢 عدد جدید را بفرستید:",
                       [[btn("🔙 انصراف", "hfsetch")]])
        elif data == "settings":
            self._settings_home(chat, mid)
        elif data == "help":
            self._help_list(chat, mid)
        elif data.startswith("help:"):
            self._help_show(chat, mid, data[5:])
        elif data == "arvan":
            self._arvan_home(chat, mid)
        elif data == "arvanacc":
            self._arvan_accs_page(chat, mid)
        elif data == "arvanaccadd":
            self._show(chat, mid,
                       "🔑 افزودن اکانت آروان\n\nکلید ماشین‌یوزر را از پنل آروان "
                       "(Settings → IAM → Machine users) بسازید و همین‌جا بفرستید.",
                       [[btn("❌ انصراف", "arvanacc")]])
            self.store.put_json(f"pend:{chat}", {"type": "arvan_key"}, 600)
        elif data.startswith("arvanaccdel:"):
            self._arvan_acc_del_ask(chat, mid, data[12:])
        elif data.startswith("arvanaccdelok:"):
            self._arvan_acc_del_do(chat, mid, data[15:])
        elif data == "arvdoms":
            self._arvan_doms(chat, mid)
        elif data.startswith("arvdom:"):
            _, acc, dom = data.split(":", 2)
            self._arvan_recs(chat, mid, int(acc), dom, 0)
        elif data.startswith("arvrec:"):
            _, acc, dom, rid = data.split(":", 3)
            self._arvan_rec(chat, mid, int(acc), dom, rid)
        elif data.startswith("arvev:"):
            _, acc, dom, rid = data.split(":", 3)
            self.store.put_json(f"pend:{chat}",
                                {"type": "arvan_value", "acc": int(acc),
                                 "dom": dom, "rid": rid}, 900)
            self._show(chat, mid, "✏️ مقدار جدید را بفرستید:",
                       [[btn("❌ انصراف", "arvan")]])
        elif data.startswith("arvdel:"):
            _, acc, dom, rid = data.split(":", 3)
            self._show(chat, mid, "⚠️ رکورد حذف شود؟",
                       [[btn("✅ بله", f"arvdelok:{acc}:{dom}:{rid}"),
                         btn("❌ انصراف", "arvan")]])
        elif data.startswith("arvdelok:"):
            _, acc, dom, rid = data.split(":", 3)
            self._arvan_del_do(chat, mid, int(acc), dom, rid)
        elif data.startswith("arvaadd:"):
            _, acc, dom = data.split(":", 2)
            self.store.put_json(f"pend:{chat}",
                                {"type": "arvan_add", "acc": int(acc),
                                 "dom": dom}, 900)
            self._show(chat, mid,
                       "➕ رکورد جدید — نوع و نام و مقدار را با فاصله بفرست:\n"
                       "<code>A sub 1.2.3.4</code>",
                       [[btn("❌ انصراف", "arvan")]])
        elif data.startswith("arvcloud:"):
            _, acc, dom, rid = data.split(":", 3)
            self._arvan_cloud(chat, mid, int(acc), dom, rid)
        elif data == "um":
            self._um_home(chat, mid)
        elif data == "um:toggle":
            from monitors import um_cfg, um_save_cfg
            cfg = um_cfg(self.store)
            cfg["enabled"] = not cfg.get("enabled")
            um_save_cfg(self.store, cfg)
            self._um_home(chat, mid)
        elif data in ("um:h", "um:d", "um:c", "um:min"):
            fld = {"um:h": ("hourlyGb", "آستانه ساعتی (GB)"),
                   "um:d": ("dailyGb", "آستانه روزانه (GB)"),
                   "um:c": ("cooldownH", "فاصله تکرار هشدار (ساعت)"),
                   "um:min": ("minGb", "حداقل مصرف اسنپ‌شات (GB)")}
            key, label = fld[data]
            self.store.put_json(f"pend:{chat}",
                                {"type": "um_num", "key": key}, 600)
            self._show(chat, mid, f"🔢 {label} را بفرستید:",
                       [[btn("🔙 انصراف", "um")]])
        elif data == "um:report":
            from monitors import um_cfg, um_save_cfg
            cfg = um_cfg(self.store)
            cfg["report"] = not cfg.get("report")
            um_save_cfg(self.store, cfg)
            self._um_home(chat, mid)
        elif data == "nd":
            self._nd_home(chat, mid)
        elif data == "ndmk":
            panels = self._pg_panels()
            kb = [[btn(f'🖥 {p.get("name") or p.get("url")}', f"ndmk:{i}")]
                  for i, p in enumerate(panels)]
            kb.append([btn("🔙 بازگشت", "nd"),
                       btn("🏠 خانه", "menu")])
            self._show(chat, mid, "🖥 برای کدام پنل مانیتور بسازم؟", kb)
        elif data.startswith("ndmk:"):
            self._nd_mk_interval(chat, mid, int(data[5:]))
        elif data.startswith("ndset:"):
            self._nd_interval_do(chat, mid, data[6:])
        elif data == "nddelmenu":
            from monitors import get_node_monitors
            mons = get_node_monitors(self.store)
            kb = [[btn(f'🗑 {m.get("name") or m.get("id")}',
                       f'nddel:{m.get("id")}')] for m in mons]
            kb.append([btn("🔙 بازگشت", "nd")])
            self._show(chat, mid, "🗑 کدام مانیتور حذف شود؟", kb)
        elif data.startswith("nddel:"):
            self._nd_del_ask(chat, mid, data[6:])
        elif data.startswith("nddy:"):
            self._nd_del_do(chat, mid, data[5:])
        elif data.startswith("ndtg:"):
            self._nd_toggle(chat, mid, data[5:])
        elif data == "pghook":
            self._pghook_home(chat, mid)
        elif data == "pgtoggle":
            import alerts as _al
            cfg = _al.get_cfg(self.store)
            cfg["enabled"] = not cfg.get("enabled", True)
            _al.save_cfg(self.store, cfg)
            self._pghook_home(chat, mid)
        elif data == "pgsilent":
            import alerts as _al
            cfg = _al.get_cfg(self.store)
            cfg["silent_me"] = not cfg.get("silent_me", False)
            _al.save_cfg(self.store, cfg)
            self._pghook_home(chat, mid)
        elif data.startswith("pgpanel:"):
            self._pgpanel(chat, mid, int(data[8:]))
        elif data.startswith("pgregen:"):
            import alerts as _al
            import secrets as _secrets
            cfg = _al.get_cfg(self.store)
            pid = str(self._pg_panels()[int(data[8:])].get("url"))
            cfg.setdefault("panels", {}).setdefault(pid, {})["secret"] = \
                _secrets.token_hex(12)
            _al.save_cfg(self.store, cfg)
            self._pgpanel(chat, mid, int(data[8:]))
        elif data.startswith("pgtest:"):
            self._pghook_test(chat, mid, int(data[7:]))
        elif data.startswith("pgdash:"):
            self.store.put_json(f"pend:{chat}",
                                {"type": "pg_dashpath",
                                 "panel": int(data[7:])}, 600)
            self._show(chat, mid,
                       "🛣 مسیر داشبورد این پنل را بفرست (مثلاً `/dashboard` یا `/fxeybo`).\nبرای برگشت به پیش‌فرض (`/dashboard`) یک خط «-» بفرست.",
                       [[btn("🔙 انصراف", f'pgpanel:{data[7:]}')]])
        elif data.startswith("pgrenewbot:"):
            self.store.put_json(f"pend:{chat}",
                                {"type": "pg_renewbot",
                                 "panel": int(data[11:])}, 600)
            self._show(chat, mid,
                       "🤖 آیدی ربات داخلی همین پنل را بفرست (بدون @، مثلاً `allmarzbot`).\n\nبرای حذف (فقط دکمه پنل بماند) یک خط «-» بفرست.",
                       [[btn("🔙 انصراف", f'pgpanel:{data[11:]}')]])
        elif data.startswith("pgrecur:"):
            self._pgrecur_ask(chat, mid, int(data[8:]))
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
            r = self._cf_find(acc, zid, rid)
            self._show(chat, mid,
                       "✏️ مقدار جدید را بفرستید (IP یا هدف CNAME):\n\n"
                       f'📛 سابی که تغییر می‌کند: <code>{esc((r or {}).get("name", ""))}</code>\n\n'
                       "🔴 مقدار فعلی که عوض می‌شود:\n"
                       f'<code>{esc((r or {}).get("content", ""))}</code>',
                       [[btn("⬅️ انصراف", f"cfr:{rid}")]])
        elif data.startswith("cfadd:"):
            _, acc, zid = data.split(":")
            self._cf_add_start(chat, mid, int(acc), zid)
        elif data.startswith("cfat:"):
            _, acc, zid, rtype = data.split(":")
            if rtype not in ("A", "AAAA", "CNAME", "TXT", "MX", "NS"):
                self._show(chat, mid, "❌ نوع رکورد نامعتبر است.", home_kb())
            else:
                self.store.put_json(f"pend:{chat}",
                                    {"type": "ar_name", "acc": int(acc),
                                     "zid": zid, "rtype": rtype}, 600)
                self._show(chat, mid,
                           f"➕ ساخت رکورد {rtype}\n\nنام رکورد را بفرستید (مثلاً www یا @):",
                           [[btn("⬅️ انصراف", f"cfz:{acc}:{zid}")]])
        elif data == "lb":
            self._lb_zones(chat, mid, "all", 0)
        elif data.startswith("lbzf:"):
            p = data.split(":")
            self._lb_zones(chat, mid, p[1], int(p[2]) if len(p) > 2
                           and p[2].lstrip("-").isdigit() else 0)
        elif data.startswith("lbacc:"):
            self._lb_zones(chat, mid, data[6:], 0)
        elif data.startswith("lbzp:"):
            p = data.split(":")
            self._lb_zone_groups(chat, mid, int(p[1]), p[2],
                                 int(p[3]) if len(p) > 3 else 0)
        elif data.startswith("lbz:"):
            p = data.split(":")
            self._lb_zone_groups(chat, mid, int(p[1]), p[2],
                                 int(p[3]) if len(p) > 3 else 0)
        elif data.startswith("lbgopen:"):
            p = data.split(":", 3)
            self._lb_open_group(chat, mid, int(p[1]), p[2], p[3])
        elif data.startswith("lbs:"):
            self._lb_settings(chat, mid, data[4:])
        elif data.startswith("lbsr:"):
            self._lb_settings_gid(chat, mid, data[5:])
        elif data.startswith("lbss:"):
            self._lb_spare_home(chat, mid, data[5:])
        elif data.startswith("lbsh:"):
            self._lb_spare_home_gid(chat, mid, data[5:])
        elif data.startswith("lbsm:"):
            _, gid, idx = data.split(":")
            self._lb_spare_menu(chat, mid, gid, int(idx))
        elif data.startswith("lbw:"):
            _, gid, idx = data.split(":")
            self._lb_weight_ask(chat, mid, gid, int(idx))
        elif data.startswith("lbt:"):
            _, gid, idx = data.split(":")
            self._lb_toggle_ask(chat, mid, gid, int(idx))
        elif data.startswith("lbtc:"):
            _, gid, idx = data.split(":")
            self._lb_toggle_do(chat, mid, gid, int(idx))
        elif data.startswith("lbadd:"):
            self._lb_add_start(chat, mid, data[6:])
        elif data.startswith("lbp:"):
            _, gid, onoff = data.split(":")
            self._lb_proxy_all(chat, mid, gid, onoff == "on")
        elif data.startswith("lbsset:"):
            _, gid, idx = data.split(":")
            self.store.put_json(f"pend:{chat}",
                                {"type": "lb_spare", "gid": gid,
                                 "idx": int(idx)}, 600)
            self._show(chat, mid,
                       "🛟 مقدار زاپاس را بفرستید (آی‌پی یا هاست‌نیم):",
                       [[btn("❌ انصراف", f"lbsr:{gid}")]])
        elif data.startswith("lbsgo:"):
            _, gid, idx = data.split(":")
            self._lb_replace(chat, mid, gid, int(idx), False)
        elif data.startswith("lbsgoc:"):
            _, gid, idx = data.split(":")
            self._lb_replace(chat, mid, gid, int(idx), True)
        elif data.startswith("lbsdel:"):
            _, gid, idx = data.split(":")
            self._lb_spare_del(chat, mid, gid, int(idx))
        elif data == "selmode":
            sess = self._cf_sess(chat)
            if not sess.get("zid"):
                self._show(chat, mid, "⏳ نشست منقضی شد.", home_kb())
            else:
                sess["sel"] = []
                self._cf_save_sess(chat, sess)
                self._cf_records(chat, mid, sess.get("acc", 0),
                                 sess["zid"], 0)
        elif data.startswith("sel:"):
            rid = data[4:]
            sess = self._cf_sess(chat)
            if not sess.get("zid"):
                self._show(chat, mid, "⏳ نشست منقضی شد.", home_kb())
            else:
                sel = set(sess.get("sel") or [])
                if rid in sel:
                    sel.discard(rid)
                else:
                    sel.add(rid)
                sess["sel"] = sorted(sel)
                self._cf_save_sess(chat, sess)
                self._cf_records(chat, mid, sess.get("acc", 0),
                                 sess["zid"], sess.get("pg", 0))
        elif data.startswith("selp:"):
            pg = int(data.split(":")[1])
            sess = self._cf_sess(chat)
            if not sess.get("zid"):
                self._show(chat, mid, "⏳ نشست منقضی شد.", home_kb())
            else:
                sess["pg"] = pg
                self._cf_save_sess(chat, sess)
                self._cf_records(chat, mid, sess.get("acc", 0),
                                 sess["zid"], pg)
        elif data == "seldone":
            sess = self._cf_sess(chat)
            sess.pop("sel", None)
            self._cf_save_sess(chat, sess)
            if sess.get("zid"):
                self._cf_records(chat, mid, sess.get("acc", 0),
                                 sess["zid"], sess.get("pg", 0))
            else:
                self._show(chat, mid, "⏳ نشست منقضی شد.", home_kb())
        elif data == "bulkdel":
            sess = self._cf_sess(chat)
            n = len(sess.get("sel") or [])
            if not n:
                self._show(chat, mid, "⚠️ چیزی انتخاب نشده.", home_kb())
            else:
                self._show(chat, mid, f"⚠️ {n} رکورد حذف شود؟",
                           [[btn("✅ بله، حذف کن", "bulkdelok"),
                             btn("❌ انصراف", "seldone")]])
        elif data == "bulkdelok":
            self._bulk_del_do(chat, mid)
        elif data == "bulkedit":
            sess = self._cf_sess(chat)
            if not (sess.get("sel") or []):
                self._show(chat, mid, "⚠️ چیزی انتخاب نشده.", home_kb())
            else:
                self.store.put_json(f"pend:{chat}", {"type": "bulkedit"}, 600)
                self._show(chat, mid, "✏️ مقدار جدید همه را بفرستید:",
                           [[btn("⬅️ انصراف", "seldone")]])
        elif data == "bulktype":
            kb = grid3([btn(t, f"bulktypev:{t}")
                        for t in ("A", "AAAA", "CNAME")])
            kb.append([btn("❌ انصراف", "seldone")])
            self._show(chat, mid, "🔄 نوع جدید همه را انتخاب کنید:", kb)
        elif data.startswith("bulktypev:"):
            self._bulk_type_do(chat, mid, data.split(":")[1])
        elif data == "favsel":
            sess = self._cf_sess(chat)
            self._fav_add_many(chat, mid, sess)
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

    # ---------- Settings & help ----------

    GUIDES = {
        "cf": ("☁️ کلودفلر", "دامنه‌ها را ببین، رکورد بساز/ویرایش/حذف کن، پروکسی و TTL را درجا عوض کن. جست‌وجو در همه رکوردها هم هست."),
        "hz": ("🇩🇪 هتزنر", "سرورها را ببین، روشن/خاموش/ریبوت کن، رمز ریست کن، اسنپ‌شات بگیر، IPv4 جایگزین کن، سرور بساز."),
        "ln": ("🟢 لینود", "مثل هتزنر: لیست، پاور، ریبوت، تغییرنام، حذف."),
        "arvan": ("🇮🇷 آروان", "دامنه‌ها و رکوردهای DNS آروان + ابری (CDN)."),
        "pg": ("🎛 پنل پاسارگارد", "کاربران هر پنل با درصد مصرف و انقضا؛ هشدار حجم/انقضا خودکار هم از همین پنل‌ها می‌آید."),
        "rem": ("⏰ یادآورها", "متن + تاریخ شمسی (مثل 1404/06/20 14:30) بده تا سر ساعت خبرت کنم."),
    }

    def _settings_home(self, chat: int, mid: int = 0) -> None:
        self._show(chat, mid,
                   f"⚙️ تنظیمات و راهنما\n\n🤖 نسخه سرور: <code>{VERSION}</code>\n"
                   "اجرا کامل روی سرور خودت؛ بدون KV و ورکر.",
                   [[btn("📖 راهنمای بخش‌ها", "help")],
                    [btn("🔙 بازگشت", "menu"),
                     btn("🏠 خانه", "menu")]])

    def _help_list(self, chat: int, mid: int = 0) -> None:
        kb = [[btn(v[0], f"help:{k}")] for k, v in self.GUIDES.items()]
        kb.append([btn("🔙 بازگشت", "settings"),
                   btn("🏠 خانه", "menu")])
        self._show(chat, mid, "📖 راهنمای بخش‌ها:", kb)

    def _help_show(self, chat: int, mid: int, key: str) -> None:
        g = self.GUIDES.get(key)
        if not g:
            self._show(chat, mid, "❌", home_kb())
            return
        self._show(chat, mid, f"{g[0]}\n\n{g[1]}",
                   [[btn("🔙 بازگشت", "help"),
                     btn("🏠 خانه", "menu")]])

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
        sess = self._cf_sess(chat)
        sess.update(acc=acc, zid=zid)
        self._cf_save_sess(chat, sess)
        sel = sess.get("sel")
        if not recs:
            self._show(chat, mid, f"📭 رکوردی برای {zname} یافت نشد.",
                       [[btn("➕ افزودن", f"cfadd:{acc}:{zid}")],
                        [btn("⚙️ تنظیمات", "zset")],
                        [btn("🔙 بازگشت", "zones"),
                         btn("🏠 خانه", "menu")]])
            return
        pages = max(1, (len(recs) + PAGE - 1) // PAGE)
        pg = max(0, min(pg, pages - 1))
        sess["pg"] = pg
        self._cf_save_sess(chat, sess)
        labels = self._dup_labels(recs, zname)
        rows = []
        for r in recs[pg * PAGE:(pg + 1) * PAGE]:
            if sel is None:
                rows.append(btn(labels[r.get("id")], f'cfr:{r.get("id")}'))
            else:
                mark = "✅" if r.get("id") in sel else "⬜"
                rows.append(btn(f"{mark} {labels[r.get('id')]}",
                                f'sel:{r.get("id")}'))
        kb = grid3(rows)
        if sel is not None:
            if sel:
                kb.append([btn("🗑 حذف انتخاب‌ها", "bulkdel"),
                           btn("✏️ تغییر مقدار", "bulkedit")])
                kb.append([btn("🔄 تغییر نوع", "bulktype")])
                kb.append([btn("⭐ افزودن به منتخب‌ها", "favsel")])
            else:
                kb.append([btn("❌ لغو انتخاب", "seldone")])
            nav = [btn("◀️", f"selp:{pg - 1}") if pg > 0 else EMPTY_BTN,
                   btn(f"📄 {pg + 1}/{pages}", "noop"),
                   btn("▶️", f"selp:{pg + 1}") if pg < pages - 1
                   else EMPTY_BTN]
            kb.append(nav)
            title = f"🗂 {zname} — {len(sel)} انتخاب شده"
            self._show(chat, mid, title, kb)
            return
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

    def _bulk_del_do(self, chat: int, mid: int) -> None:
        sess = self._cf_sess(chat)
        sel = sess.get("sel") or []
        if not sel or not sess.get("zid"):
            self._show(chat, mid, "⚠️ چیزی انتخاب نشده.", home_kb())
            return
        try:
            client = self._cf_client(sess.get("acc", 0))
            ok, fail = 0, 0
            for rid in sel:
                try:
                    client.del_record(sess["zid"], rid)
                    ok += 1
                except CfError:
                    fail += 1
        except IndexError as e:
            self._show(chat, mid, f"❌ {esc(e)}", home_kb())
            return
        sess.pop("sel", None)
        self._cf_save_sess(chat, sess)
        self.bot.send(chat, f"✅ {ok} حذف شد." + (f" ⚠️ {fail} ناموفق." if fail else ""))
        self._cf_records(chat, 0, sess.get("acc", 0), sess["zid"],
                         sess.get("pg", 0))

    def _bulk_type_do(self, chat: int, mid: int, rtype: str) -> None:
        sess = self._cf_sess(chat)
        sel = sess.get("sel") or []
        if not sel or not sess.get("zid"):
            self._show(chat, mid, "⚠️ چیزی انتخاب نشده.", home_kb())
            return
        try:
            client = self._cf_client(sess.get("acc", 0))
            recs = client.records(sess["zid"]) or []
        except (IndexError, CfError) as e:
            self._show(chat, mid, f"❌ {esc(e)}", home_kb())
            return
        ok, skip = 0, 0
        for rid in sel:
            r = next((x for x in recs if x.get("id") == rid), None)
            if not r:
                skip += 1
                continue
            if _value_hint(str(r.get("content") or ""), rtype) is not None:
                skip += 1
                continue
            try:
                client.update_record(sess["zid"], r, type=rtype)
                ok += 1
            except CfError:
                skip += 1
        sess.pop("sel", None)
        self._cf_save_sess(chat, sess)
        self.bot.send(chat, f"✅ نوع {ok} رکورد عوض شد." + (f" ⚠️ {skip} رد شد." if skip else ""))
        self._cf_records(chat, 0, sess.get("acc", 0), sess["zid"],
                         sess.get("pg", 0))

    def _fav_add_many(self, chat: int, mid: int, sess: dict) -> None:
        sel = sess.get("sel") or []
        if not sel or not sess.get("zid"):
            self._show(chat, mid, "⚠️ چیزی انتخاب نشده.", home_kb())
            return
        try:
            client = self._cf_client(sess.get("acc", 0))
            zname = next((z["name"] for z in client.zones()
                          if z["id"] == sess["zid"]), sess["zid"])
            recs = client.records(sess["zid"]) or []
        except (IndexError, CfError) as e:
            self._show(chat, mid, f"❌ {esc(e)}", home_kb())
            return
        favs = self._favs()
        have = {f.get("rid") for f in favs}
        n = 0
        for rid in sel:
            if rid in have:
                continue
            r = next((x for x in recs if x.get("id") == rid), None)
            if not r:
                continue
            favs.append({"acc": sess.get("acc", 0), "zid": sess["zid"],
                         "rid": rid,
                         "name": self._rec_label(str(r.get("name") or ""),
                                                 r.get("type"), zname)})
            n += 1
        self.store.put_json("favs", favs)
        sess.pop("sel", None)
        self._cf_save_sess(chat, sess)
        self.bot.send(chat, f"✅ {n} مورد به منتخب اضافه شد.")
        self._cf_records(chat, 0, sess.get("acc", 0), sess["zid"],
                         sess.get("pg", 0))

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
        ]
        if str(r.get("type") or "").upper() in ("A", "AAAA", "CNAME"):
            kb.append([btn("🛟 آیپی زاپاس", f"lbss:{rid}", "success"),
                       btn("⚙️ تنظیمات لود بالانسر", f"lbs:{rid}",
                           "success")])
        kb += [
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

    def _ar_create(self, chat: int, pend: dict, content: str,
                   priority) -> None:
        hint = _value_hint(content, str(pend.get("rtype", "A")).upper())
        if hint:
            self.bot.send(chat,
                          f"❌ {hint}\n\nپیام اصلاح‌شده را دوباره بفرستید.",
                          [[btn("⬅️ انصراف",
                                f'cfz:{pend.get("acc", 0)}:{pend.get("zid", "")}')]])
            return
        try:
            self._cf_client(pend.get("acc", 0)).add_record(
                pend["zid"], pend.get("rtype", "A"), pend.get("name", ""),
                content, 1, False, priority)
        except (IndexError, CfError) as e:
            self.bot.send(chat, f"❌ ثبت نشد: {esc(e)}")
            return
        self.store.delete(f"pend:{chat}")
        self.bot.send(chat, "✅ رکورد ثبت شد.")
        self._cf_records(chat, 0, pend.get("acc", 0), pend["zid"], 0)

    def _cf_add_start(self, chat: int, mid: int, acc: int,
                    zid: str) -> None:
        types = ["A", "AAAA", "CNAME", "TXT", "MX", "NS"]
        kb = grid3([btn(t, f"cfat:{acc}:{zid}:{t}") for t in types])
        kb.append([btn("🔙 بازگشت", f"cfz:{acc}:{zid}"),
                   btn("🏠 خانه", "menu")])
        try:
            zname = next((z["name"] for z in
                          self._cf_client(acc).zones()
                          if z["id"] == zid), zid)
        except (IndexError, CfError):
            zname = zid
        self._show(chat, mid,
                   f"➕ ساخت رکورد در {zname}\n\nنوع رکورد را انتخاب کنید:",
                   kb)

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
        elif pend.get("type") == "ar_name":
            if not text.strip():
                self.bot.send(chat, "⚠️ نام رکورد معتبر نیست.")
                return
            pend = dict(pend, type="ar_content", name=text.strip())
            self.store.put_json(f"pend:{chat}", pend, 600)
            rt = pend.get("rtype", "A")
            if rt == "MX":
                msg = "📮 هاست سرور ایمیل را بفرستید (مثلاً mail.example.com):"
            elif rt == "TXT":
                msg = "📝 متن رکورد TXT را بفرستید:"
            elif rt == "NS":
                msg = "🔤 هاست نیم‌سرور را بفرستید (مثلاً ns1.example.com):"
            else:
                msg = "🔤 حالا مقدار IP رکورد را بفرستید:"
            self.bot.send(chat, msg,
                          [[btn("⬅️ انصراف",
                                f'cfz:{pend.get("acc", 0)}:{pend.get("zid", "")}')]])
        elif pend.get("type") == "ar_content":
            if pend.get("rtype") == "MX":
                pend = dict(pend, type="ar_priority",
                            content=text.strip())
                self.store.put_json(f"pend:{chat}", pend, 600)
                self.bot.send(chat, "🔢 اولویت MX را بفرستید (عدد؛ خالی = ۱۰):",
                              [[btn("⬅️ انصراف",
                                    f'cfz:{pend.get("acc", 0)}:{pend.get("zid", "")}')]])
                return
            self._ar_create(chat, pend, text.strip(), None)
        elif pend.get("type") == "ar_priority":
            pr = int(text.strip()) if text.strip().isdigit() else 10
            self._ar_create(chat, dict(pend, priority=pr),
                            pend.get("content", ""), pr)
        elif pend.get("type") == "bulkedit":
            sess = self._cf_sess(chat)
            sel = sess.get("sel") or []
            if not sel or not sess.get("zid"):
                self.store.delete(f"pend:{chat}")
                self.bot.send(chat, "⚠️ چیزی انتخاب نشده.")
                return
            try:
                client = self._cf_client(sess.get("acc", 0))
                recs = client.records(sess["zid"]) or []
            except (IndexError, CfError) as e:
                self.bot.send(chat, f"❌ {esc(e)}")
                return
            ok, skip = 0, 0
            for rid in sel:
                r = next((x for x in recs if x.get("id") == rid), None)
                if not r:
                    skip += 1
                    continue
                if _value_hint(text.strip(),
                               str(r.get("type") or "").upper()) is not None:
                    skip += 1
                    continue
                try:
                    client.update_record(sess["zid"], r,
                                         content=text.strip())
                    ok += 1
                except CfError:
                    skip += 1
            self.store.delete(f"pend:{chat}")
            sess.pop("sel", None)
            self._cf_save_sess(chat, sess)
            self.bot.send(chat, f"✅ {ok} رکورد عوض شد." + (f" ⚠️ {skip} رد شد." if skip else ""))
            self._cf_records(chat, 0, sess.get("acc", 0), sess["zid"],
                             sess.get("pg", 0))
        elif pend.get("type") == "cfedit":
            r = self._cf_find(pend.get("acc", 0), pend["zid"], pend["rid"])
            if not r:
                self.store.delete(f"pend:{chat}")
                self.bot.send(chat, "❌ رکورد پیدا نشد.")
                return
            hint = _value_hint(text.strip(), str(r.get("type", "")).upper())
            if hint:
                self.bot.send(chat,
                              f"❌ {hint}\n\nپیام اصلاح‌شده را دوباره بفرستید؛ همین‌جا ویرایش می‌شود.",
                              [[btn("⬅️ انصراف", f'cfr:{pend["rid"]}')]])
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
        elif pend.get("type") == "um_num":
            from monitors import um_cfg, um_save_cfg
            try:
                v = float(text.strip())
                if v < 0:
                    raise ValueError
            except ValueError:
                self.bot.send(chat, "❌ عدد نامعتبر است.")
                return
            cfg = um_cfg(self.store)
            cfg[pend.get("key", "hourlyGb")] = v
            um_save_cfg(self.store, cfg)
            self.store.delete(f"pend:{chat}")
            self.bot.send(chat, "✅ ذخیره شد.")
            self._um_home(chat, 0)
        elif pend.get("type") == "hf_num":
            from hostfilter import get_cfg, save_cfg
            try:
                v = int(text.strip())
                assert v >= 0
            except (ValueError, AssertionError):
                self.bot.send(chat, "❌ عدد نامعتبر است.")
                return
            cfg = get_cfg(self.store)
            cfg[pend.get("key", "intervalMin")] = v
            save_cfg(self.store, cfg)
            self.store.delete(f"pend:{chat}")
            self.bot.send(chat, "✅ ذخیره شد.")
            self._hf_settings(chat, 0)
        elif pend.get("type") == "hz_srv_remark" and "remark" not in pend:
            remark = text.strip()
            if not remark or any(ch.isspace() for ch in remark):
                self.bot.send(chat,
                              "⚠️ نام سرور نباید فاصله داشته باشد. دوباره بفرستید:")
                return
            try:
                locs = Hz(self._hz_accs()[pend["acc"]]["token"]).all(
                    "/locations")
            except (IndexError, HzError) as e:
                self.store.delete(f"pend:{chat}")
                self.bot.send(chat, f"❌ {esc(e)}")
                return
            if not locs:
                self.store.delete(f"pend:{chat}")
                self.bot.send(chat, "❌ لوکیشنی پیدا نشد.")
                return
            pend["remark"] = remark
            self.store.put_json(f"pend:{chat}", pend, 600)
            kb = grid2([btn(f'{l.get("city") or l.get("name")} ({l.get("name")})',
                             f'hzsl:{pend["acc"]}:{l.get("name")}')
                        for l in locs[:40]])
            kb.append([btn("🔙 انصراف", f'hzs:{pend["acc"]}:0')])
            self.bot.send(chat, "🌍 لوکیشن (دیتاسنتر) را انتخاب کنید:", kb)
        elif pend.get("type") == "lb_weight":
            from lb import get_cfg, save_cfg
            try:
                v = float(text.strip())
                assert v >= 0
            except (ValueError, AssertionError):
                self.bot.send(chat, "❌ عدد نامعتبر است.")
                return
            sess = self.store.get_json(f'lbg:{pend["gid"]}', {}) or {}
            key = f'lb:cf:{sess.get("zid")}:{sess.get("name")}'
            cfg = get_cfg(self.store, key)
            entries = [e for e in self._lb_entries(sess) if True]
            if pend["idx"] < len(entries):
                (cfg.get("weights") or {}).__setitem__(
                    entries[pend["idx"]]["content"], v)
                cfg["weights"] = cfg.get("weights") or {}
                save_cfg(self.store, key, cfg)
            self.store.delete(f"pend:{chat}")
            self.bot.send(chat, "✅ ذخیره شد.")
            self._lb_render(chat, 0, pend["gid"])
        elif pend.get("type") == "lb_spare":
            sess = self.store.get_json(f'lbg:{pend["gid"]}', {}) or {}
            entries = [e for e in self._lb_entries(sess) if e["on"]]
            if pend["idx"] >= len(entries):
                self.store.delete(f"pend:{chat}")
                self.bot.send(chat, "❌ مورد پیدا نشد.")
                return
            v = text.strip()
            import ipaddress as _ip

            def _is_ip(x):
                try:
                    _ip.ip_address(x)
                    return True
                except ValueError:
                    return False
            typ = "A" if _is_ip(v) and "." in v else ("AAAA" if _is_ip(v) else "CNAME")
            key = f'lb:cf:{sess.get("zid")}:{sess.get("name")}'
            from lb import get_cfg, save_cfg
            cfg = get_cfg(self.store, key)
            (cfg.get("spares") or {}).__setitem__(
                entries[pend["idx"]]["content"], {"type": typ, "content": v})
            cfg["spares"] = cfg.get("spares") or {}
            save_cfg(self.store, key, cfg)
            self.store.delete(f"pend:{chat}")
            self.bot.send(chat, "✅ زاپاس ذخیره شد.")
            self._lb_spare_home_gid(chat, 0, pend["gid"])
        elif pend.get("type") == "lb_add":
            sess = self.store.get_json(f'lbg:{pend["gid"]}', {}) or {}
            parts = text.split()
            if len(parts) < 2:
                self.bot.send(chat, "❌ قالب درست نیست (مثلاً <code>A 1.2.3.4</code>).")
                return
            try:
                client = self._cf_client(sess.get("acc", 0))
                client.add_record(sess["zid"], parts[0].upper(),
                                  sess["name"], parts[1], 1, False)
                self.bot.send(chat, "✅ آی‌پی اضافه شد.")
            except (IndexError, CfError) as e:
                self.bot.send(chat, f"❌ نشد: {esc(e)}")
                return
            self.store.delete(f"pend:{chat}")
            self._lb_render(chat, 0, pend["gid"])
        elif pend.get("type") == "ssl_host":
            host = text.strip().lower().split("/")[0].split(":")[0]
            if not host or "." not in host:
                self.bot.send(chat, "❌ دامنه معتبر نیست.")
                return
            mons = [m for m in __import__("sslmon").get_monitors(self.store)]
            if host not in [m.get("host") for m in mons]:
                mons.append({"host": host})
                self.store.put_json("ssl_monitor", mons)
            self.store.delete(f"pend:{chat}")
            self.bot.send(chat, "✅ اضافه شد.")
            self._sslm_home(chat, 0)
        elif pend.get("type") == "domexp_domain":
            import re as _re
            import time as _t
            import domexpiry as de
            domain = (text.strip().lower()
                      .replace("https://", "").replace("http://", ""))
            domain = domain.split("/")[0].strip()
            if not _re.match(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$", domain):
                self.bot.send(chat,
                              "❌ نام دامنه معتبر نیست. دوباره بفرستید (مثلاً example.ir):")
                return
            lst = self._domexp_list()
            if any(x.get("domain") == domain for x in lst):
                self.bot.send(chat, "❌ این دامنه از قبل در مانیتور هست.",
                              [[btn("🗓 مانیتور انقضای دامنه", "domexpp:0")]])
                self.store.delete(f"pend:{chat}")
                return
            self.store.delete(f"pend:{chat}")
            self.bot.send(chat, "⏳ در حال استعلام انقضای دامنه…")
            r = de.fetch_domain_expiry(domain, self._domexp_cfg()["key"])
            if r.get("status") == "ok":
                lst.append({"domain": domain, "status": "ok",
                            "expiry": r.get("expiry"),
                            "added": int(_t.time() * 1000)})
                self.store.put_json("dom_expiry", lst)
                self.bot.send(chat,
                              f'✅ <code>{esc(domain)}</code> به مانیتور اضافه شد.\n'
                              f'🗓 انقضا: {fmt_jalali(r["expiry"]).split(" - ")[0]}\n'
                              f'⏳ باقی‌مانده: {de.remaining(r["expiry"])}',
                              [[btn("🗓 مانیتور انقضای دامنه", "domexpp:0"),
                                btn("🔙 بازگشت", "menu")]])
            else:
                st = r.get("status")
                lst.append({"domain": domain, "status": st, "expiry": None,
                            "added": int(_t.time() * 1000)})
                self.store.put_json("dom_expiry", lst)
                reason = ("ایرنیک تاریخ این دامنه را مخفی کرده. برای استعلام خودکار «🔑 ثبت کلید API» را بزن."
                          if st == "hidden" else
                          "دامنه در ایرنیک ثبت نیست." if st == "not_found"
                          else "دامنه آزاد/ثبت‌نشده است." if st == "available"
                          else "خطای API: " + str(r.get("message") or "کلید/اعتبار را بررسی کن.")
                          if st == "api_error" else
                          "استعلام ناموفق بود؛ بعداً با «🔄 استعلام دوبارهٔ همه» امتحان کن.")
                self.bot.send(chat,
                              f'➕ <code>{esc(domain)}</code> اضافه شد، اما تاریخ انقضا پیدا نشد.\n'
                              f'ℹ️ {reason}\n\n'
                              "از دکمهٔ «🗓 ثبت تاریخ دستی» می‌توانی تاریخ را خودت وارد کنی.",
                              [[btn("🗓 ثبت تاریخ دستی", "domexpcal"),
                                btn("🗓 مانیتور انقضای دامنه", "domexpp:0")]])
        elif pend.get("type") == "domexp_key":
            import domexpiry as de
            key = text.strip()
            self.store.delete(f"pend:{chat}")
            if not key or key == "-":
                self.store.put_json("dom_expiry_cfg",
                                    {"provider": "whoisxmlapi_domaininfo",
                                     "key": ""})
                self.bot.send(chat, "🗑 کلید API حذف شد.",
                              [[btn("🗓 مانیتور انقضای دامنه", "domexpp:0")]])
                return
            cfg = self._domexp_cfg()
            cfg["key"] = key
            self.store.put_json("dom_expiry_cfg", cfg)
            self.bot.send(chat, "⏳ کلید ذخیره شد. در حال تست روی pcapps.ir…")
            r = de.whois_xml_domain_info("pcapps.ir", key)
            if r.get("status") == "ok":
                self.bot.send(chat,
                              "✅ کلید API معتبر است.\n"
                              f'🗓 انقضای pcapps.ir: {fmt_jalali(r["expiry"]).split(" - ")[0]}\n'
                              f'⏳ باقی‌مانده: {de.remaining(r["expiry"])}',
                              [[btn("🗓 مانیتور انقضای دامنه", "domexpp:0"),
                                btn("🔙 بازگشت", "menu")]])
            elif r.get("status") == "api_error":
                self.bot.send(chat,
                              f'⚠️ کلید ذخیره شد اما تست ناموفق بود:\n<code>{esc(r.get("message"))}</code>',
                              [[btn("🗓 مانیتور انقضای دامنه", "domexpp:0")]])
            else:
                self.bot.send(chat,
                              "⚠️ کلید ذخیره شد، اما برای pcapps.ir تاریخی برنگشت (ممکن است در دیتابیس تاریخی هم نباشد).",
                              [[btn("🗓 مانیتور انقضای دامنه", "domexpp:0")]])
        elif pend.get("type") == "domexp_date":
            import re as _re
            import domexpiry as de
            s = text.translate(str.maketrans(
                "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
                "01234567890123456789")).strip()
            m = _re.match(r"^(\d{4})[\/\-.](\d{1,2})[\/\-.](\d{1,2})$", s)
            if not m:
                self.bot.send(chat, "❌ قالب درست نیست. مثال: 1405/06/20")
                return
            try:
                expiry = parse_jalali_datetime(
                    f"{m[1]}/{int(m[2])}/{int(m[3])} 12:00")
            except ValueError:
                self.bot.send(chat, "❌ تاریخ نامعتبر است. دوباره بفرستید (مثلاً 1405/06/20):")
                return
            if expiry <= de._now_ms():
                self.bot.send(chat, "⚠️ این تاریخ گذشته است. تاریخ انقضای آینده را بفرستید:")
                return
            self.store.delete(f"pend:{chat}")
            lst = self._domexp_list()
            item = next((x for x in lst
                         if x.get("domain") == pend.get("domain")), None)
            if not item:
                self.bot.send(chat, "❌ دامنه در مانیتور پیدا نشد.",
                              [[btn("🗓 مانیتور انقضای دامنه", "domexpp:0")]])
                return
            item["manual"] = True
            item["status"] = "ok"
            item["expiry"] = expiry
            item["last_alert_day"] = None
            self.store.put_json("dom_expiry", lst)
            self.bot.send(chat,
                          f'✅ تاریخ انقضای <code>{esc(pend.get("domain"))}</code> ثبت شد.\n'
                          f'🗓 انقضا: {fmt_jalali(expiry).split(" - ")[0]}\n'
                          f'⏳ باقی‌مانده: {de.remaining(expiry)}',
                          [[btn("🗓 مانیتور انقضای دامنه", "domexpp:0"),
                            btn("🔙 بازگشت", "menu")]])
        elif pend.get("type") == "pg_search":
            self.store.delete(f"pend:{chat}")
            self._pg_users(chat, 0, pend["panel"], 0, text.strip().lower())
        elif pend.get("type") == "srv_add_host":
            host = text.strip().split()[0]
            if not host:
                self.bot.send(chat, "❌ هاست معتبر نیست.")
                return
            self.store.put_json(f"pend:{chat}",
                                {"type": "srv_add_user",
                                 "host": host}, 600)
            self.bot.send(chat, "👤 یوزر SSH؟ (پیش‌فرض root — بنویسید `root`):")
        elif pend.get("type") == "srv_add_user":
            self.store.put_json(f"pend:{chat}",
                                {"type": "srv_add_secret",
                                 "host": pend["host"],
                                 "user": text.strip() or "root"}, 600)
            self.bot.send(chat, "🔒 رمز عبور (یا کلید خصوصی SSH) را بفرستید:")
        elif pend.get("type") == "srv_add_secret":
            import time as _t
            import random as _r
            lst = self._srv_list()
            lst.append({"id": f's{int(_t.time())}{_r.randint(10, 99)}',
                        "name": pend["host"], "host": pend["host"],
                        "user": pend.get("user") or "root",
                        "port": 22, "auth": "key"
                        if text.strip().startswith("-----BEGIN") else "pass",
                        "secret": text.strip()})
            self.store.put_json("srv_list", lst)
            self.store.delete(f"pend:{chat}")
            self.bot.send(chat, "✅ سرور اضافه شد.")
            self._srv_home(chat, 0)
        elif pend.get("type") == "srv_bulk":
            import time as _t
            import random as _r
            lst = self._srv_list()
            n = 0
            for ln in text.splitlines():
                p = ln.split()
                if not p:
                    continue
                lst.append({"id": f's{int(_t.time())}{_r.randint(10, 99)}{n}',
                            "name": p[0], "host": p[0],
                            "user": p[1] if len(p) > 1 else "root",
                            "port": int(p[3]) if len(p) > 3 and p[3].isdigit() else 22,
                            "auth": "pass",
                            "secret": p[2] if len(p) > 2 else ""})
                n += 1
            self.store.put_json("srv_list", lst)
            self.store.delete(f"pend:{chat}")
            self.bot.send(chat, f"✅ {n} سرور اضافه شد.")
            self._srv_home(chat, 0)
        elif pend.get("type") == "srvmon_num":
            from monitors import srvmon_cfg, srvmon_save
            try:
                v = float(text.strip())
                assert v > 0
            except (ValueError, AssertionError):
                self.bot.send(chat, "❌ عدد نامعتبر است.")
                return
            key = {"cpu": "cpuPct", "mem": "memPct", "disk": "diskPct",
                   "cool": "cooldownMin"}.get(pend.get("key", ""), "cpuPct")
            cfg = srvmon_cfg(self.store)
            cfg[key] = v
            srvmon_save(self.store, cfg)
            self.store.delete(f"pend:{chat}")
            self.bot.send(chat, "✅ ذخیره شد.")
            self._srvmon_home(chat, 0)
        elif pend.get("type") == "arvan_key":
            key = norm_key(text.strip())
            if not key:
                self.bot.send(chat, "❌ کلید معتبر نیست.")
                return
            try:
                Arvan(key).call("/domains?per_page=1")
            except ArvanError as e:
                self.bot.send(chat, f"❌ کلید قبول نشد:\n{esc(e)}")
                return
            accs = self._arvan_acc_list()
            accs.append({"name": f'آروان {len(accs) + 1}', "token": key})
            self.store.put_json("arvan_accounts", accs)
            self.store.delete(f"pend:{chat}")
            self.bot.send(chat, "✅ اکانت اضافه شد.",
                          [[btn("🌐 دامنه‌ها", "arvdoms"),
                            btn("👤 اکانت‌ها", "arvanacc")]])
        elif pend.get("type") == "arvan_value":
            try:
                Arvan(self._arvan_acc_list()[pend["acc"]]["token"]).call(
                    f'/domains/{pend["dom"]}/dns-records/{pend["rid"]}',
                    "PUT", {"value": text.strip()})
                self.bot.send(chat, "✅ مقدار عوض شد.")
            except (IndexError, ArvanError) as e:
                self.bot.send(chat, f"❌ نشد: {esc(e)}")
                return
            self.store.delete(f"pend:{chat}")
            self._arvan_rec(chat, 0, pend["acc"], pend["dom"], pend["rid"])
        elif pend.get("type") == "arvan_add":
            parts = text.split()
            if len(parts) < 3:
                self.bot.send(chat, "❌ قالب درست نیست.")
                return
            try:
                Arvan(self._arvan_acc_list()[pend["acc"]]["token"]).call(
                    f'/domains/{pend["dom"]}/dns-records', "POST",
                    {"type": parts[0].upper(), "name": parts[1],
                     "value": parts[2]})
                self.bot.send(chat, "✅ رکورد ثبت شد.")
            except (IndexError, ArvanError) as e:
                self.bot.send(chat, f"❌ ثبت نشد: {esc(e)}")
                return
            self.store.delete(f"pend:{chat}")
            self._arvan_recs(chat, 0, pend["acc"], pend["dom"], 0)
        elif pend.get("type") == "pg_dashpath":
            import alerts as _al
            self.store.delete(f"pend:{chat}")
            cfg = _al.get_cfg(self.store)
            try:
                pid = str(self._pg_panels()[pend["panel"]].get("url"))
            except IndexError:
                self.bot.send(chat, "❌ پنل پیدا نشد.")
                return
            cfg.setdefault("panels", {}).setdefault(pid, {})
            if text.strip() == "-":
                cfg["panels"][pid].pop("dashpath", None)
            else:
                d = text.strip()
                if not d.startswith("/"):
                    d = "/" + d
                d = d.rstrip("/") or "/dashboard"
                import re as _re
                if _re.search(r"[\s?#]", d) or len(d) > 60:
                    self.bot.send(chat,
                                  "❌ مسیر معتبر نیست (مثل `/dashboard`).")
                    return
                cfg["panels"][pid]["dashpath"] = d
            _al.save_cfg(self.store, cfg)
            self.bot.send(chat, "✅ ذخیره شد.",
                          [[btn("🔙 پنل", f'pgpanel:{pend["panel"]}'),
                            btn("🏠 خانه", "menu")]])
        elif pend.get("type") == "pg_renewbot":
            import alerts as _al
            import re as _re
            self.store.delete(f"pend:{chat}")
            cfg = _al.get_cfg(self.store)
            try:
                pid = str(self._pg_panels()[pend["panel"]].get("url"))
            except IndexError:
                self.bot.send(chat, "❌ پنل پیدا نشد.")
                return
            cfg.setdefault("panels", {}).setdefault(pid, {})
            if text.strip() == "-":
                cfg["panels"][pid].pop("renewbot", None)
            else:
                v = text.strip().lstrip("@")
                if not _re.match(r"^[A-Za-z0-9_]{3,64}$", v) or \
                   not v.lower().endswith("bot"):
                    self.bot.send(chat,
                                  "❌ آیدی ربات معتبر نیست (مثلاً `allmarzbot`).")
                    return
                cfg["panels"][pid]["renewbot"] = v
                cfg["panels"][pid]["renewbotAuto"] = False
            _al.save_cfg(self.store, cfg)
            self.bot.send(chat, "✅ ذخیره شد.",
                          [[btn("🔙 پنل", f'pgpanel:{pend["panel"]}'),
                            btn("🏠 خانه", "menu")]])
        elif pend.get("type") == "pg_recur":
            from pg import Pg as _Pg
            self.store.delete(f"pend:{chat}")
            try:
                n = int(text.strip())
                assert 60 <= n <= 86400
            except (ValueError, AssertionError):
                self.bot.send(chat, "❌ عدد بین ۶۰ تا ۸۶۴۰۰ بفرست.")
                return
            try:
                p = self._pg_panels()[pend["panel"]]
                tok = _Pg(p["url"], p["username"], p["password"]).login()
                st = _Pg(p["url"], p["username"], p["password"]).settings(tok)
                st["webhook"]["recurrent"] = n
                import json as _j
                import urllib.request as _u
                req = _u.Request(p["url"].rstrip("/") + "/api/settings",
                                 data=_j.dumps(st).encode(),
                                 headers={"Authorization": f"Bearer {tok}",
                                          "Content-Type": "application/json"},
                                 method="PUT")
                with _u.urlopen(req, timeout=25) as r:
                    ok = r.status == 200
            except Exception:
                ok = False
            self.bot.send(chat,
                          f"✅ recurrent شد {n} ثانیه." if ok
                          else "❌ ذخیره در پنل نشد.",
                          [[btn("🔙 پنل", f'pgpanel:{pend["panel"]}'),
                            btn("🏠 خانه", "menu")]])
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

    # ---------- Expiry/volume alerts ----------

    def _pghook_home(self, chat: int, mid: int = 0) -> None:
        import alerts as _al
        cfg = _al.get_cfg(self.store)
        panels = self._pg_panels()
        lines = ["🔔 هشدار انقضا و حجم پاسارگارد", ""]
        if not self.store.get("pghook_seen"):
            lines.append("👋 راهنمای شروع (فقط همین یک‌بار می‌بینی):")
            lines.append("۱️⃣ تو صفحه هر پنل، آدرس وبهوک را کپی کن و در تنظیمات webhook همان پنل بگذار (days_left و usage_percent هم همان‌جاست).")
            lines.append("۲️⃣ ربات داخلی (دکمه تمدید با ربات) معمولاً خودکار از خود پنل خوانده می‌شود؛ اگر نشد تو صفحه هر پنل دستی بده.")
            lines.append("۳️⃣ هر ادمین پنل که telegram_id داشته باشد هشدار کاربرهایش را می‌گیرد.")
            lines.append("")
            self.store.put("pghook_seen", "1")
        lines.append("وضعیت: " + ("▶️ فعال" if cfg["enabled"] else "⏸ غیرفعال (پیام نمی‌آید)"))
        lines.append("📅 آستانه‌ها در خود پنل تنظیم می‌شوند (پیشنهاد: روز ۱، حجم ۹۰٪).")
        lines.append("📦 ارسال جمعی پولی: حجم ساعتی یک‌جا، انقضا روزی یک‌بار (کرون خودش از پنل می‌خواند)؛ فقط بحران امروزی (۰ روز / ۱۰۰٪) فوری می‌آید.")
        lines.append(f'🖥 پنل‌های متصل: {len(cfg.get("panels") or {})} از {len(panels)}')
        lines.append("")
        lines.append("روش: در تنظیمات webhook هر پنل، آدرس اختصاصی‌اش را بگذار تا وقتی کاربری به آستانه رسید، پنل خودش خبر بده.")
        kb = [[btn("⏸ غیرفعال‌سازی" if cfg["enabled"] else "▶️ فعال‌سازی",
                   "pgtoggle"),
               btn("🔕 سایلنت من: روشن" if cfg.get("silent_me")
                   else "🔔 سایلنت من: خاموش", "pgsilent")]]
        for i, p in enumerate(panels):
            pid = str(p.get("url"))
            reg = pid in (cfg.get("panels") or {})
            kb.append([btn(f'{"✅ " if reg else "⬜ "}⚙️ تنظیم '
                           f'{str(p.get("name") or pid)[:22]}',
                           f"pgpanel:{i}")])
        kb.append([btn("🏠 خانه", "menu")])
        self._show(chat, mid, "\n".join(lines), kb)

    def _pgpanel(self, chat: int, mid: int, i: int) -> None:
        import alerts as _al
        import secrets as _secrets
        from pg import Pg as _Pg
        try:
            p = self._pg_panels()[i]
        except IndexError:
            self._show(chat, mid, "❌ پنل پیدا نشد.",
                       [[btn("🔙 هشدارها", "pghook"),
                         btn("🏠 خانه", "menu")]])
            return
        pid = str(p.get("url"))
        cfg = _al.get_cfg(self.store)
        pcfg = (cfg.get("panels") or {}).get(pid) or {}
        if not pcfg.get("secret"):
            pcfg["secret"] = _secrets.token_hex(12)
            cfg.setdefault("panels", {})[pid] = pcfg
            _al.save_cfg(self.store, cfg)
        base = (self._public_base() or "<server-url>").rstrip("/")
        url = base + "/pghook/" + pcfg["secret"]
        hook = None
        try:
            pgc = _Pg(p["url"], p["username"], p["password"])
            tok = pgc.login()
            st = pgc.settings(tok) if tok else None
            w = (st or {}).get("webhook") or {}
            hook = {"enable": w.get("enable") is True,
                    "days": w.get("days_left"), "usage": w.get("usage_percent"),
                    "recurrent": w.get("recurrent"),
                    "urls": [str((x or {}).get("url") or "")
                             for x in (w.get("webhooks") or [])]}
            # ربات داخلی خودکار از تنظیمات تلگرام پنل
            ttok = ((st or {}).get("telegram") or {}).get("token")
            if ttok and (not pcfg.get("renewbot") or
                         pcfg.get("renewbotAuto")):
                un = _pg_tg_username(ttok)
                if un and un != pcfg.get("renewbot"):
                    pcfg["renewbot"] = un
                    pcfg["renewbotAuto"] = True
                    cfg.setdefault("panels", {})[pid] = pcfg
                    _al.save_cfg(self.store, cfg)
        except Exception:
            pass
        dash = pcfg.get("dashpath", "/dashboard")
        botnm = pcfg.get("renewbot", "")
        lines = [f'🔔 وبهوک پنل: {p.get("name") or pid}', "",
                 "📨 آدرس وبهوک:", f"<code>{esc(url)}</code>", "",
                 "🔑 سکرت:", f'<code>{esc(pcfg["secret"])}</code>', "",
                 "آدرس بالا را در قسمت url و سکرت زیر را در قسمت secret تنظیمات webhook پنل کپی کن (days_left و usage_percent هم همان‌جاست).",
                 "", f"🛣 مسیر داشبورد: <code>{esc(dash)}</code>",
                 "🤖 ربات داخلی: " + (f"@{botnm}" + (" (خودکار)" if pcfg.get("renewbotAuto") else "")
                                        if botnm else "تعریف نشده")]
        if hook:
            ok_url = any(u.rstrip("/") == url.rstrip("/") for u in hook["urls"])
            lines.append("📡 پنل: وبهوک " +
                         ("روشن" if hook["enable"] else "❌ خاموش") +
                         (f' · روز {hook["days"]}' if hook.get("days") else "") +
                         (f' · حجم {hook["usage"]}' if hook.get("usage") else "") +
                         (f' · هر {hook["recurrent"]}ث' if hook.get("recurrent") is not None else "") +
                         " · آدرس ما " + ("✅" if ok_url else "❌ نیست"))
        kb = [[btn("🧪 پیام تستی", f"pgtest:{i}")],
              [btn("🛣 مسیر داشبورد", f"pgdash:{i}"),
               btn("🤖 ربات داخلی", f"pgrenewbot:{i}")],
              [btn("🔄 توکن جدید", f"pgregen:{i}")],
              [btn("⏱ recurrent" + (f': {hook["recurrent"]}ث' if hook and hook.get("recurrent") is not None else ""),
                   f"pgrecur:{i}")],
              [btn("🔙 هشدارها", "pghook"),
               btn("🏠 خانه", "menu")]]
        self._show(chat, mid, "\n".join(lines), kb)

    def _pghook_test(self, chat: int, mid: int, i: int) -> None:
        import alerts as _al
        try:
            p = self._pg_panels()[i]
        except IndexError:
            self._show(chat, mid, "❌ پنل پیدا نشد.",
                       [[btn("🔙 هشدارها", "pghook")]])
            return
        cfg = _al.get_cfg(self.store)
        pcfg = (cfg.get("panels") or {}).get(str(p.get("url")), {})
        try:
            _al.send_alert(self.bot, chat,
                           {"name": p.get("name"), "url": p.get("url"),
                            "renewbot": pcfg.get("renewbot", "")},
                           [{"username": "test-user", "kind": "days",
                             "value": 1, "expireTs": None, "status": "",
                             "owner": "-"}], test=True)
        except Exception as e:
            self._show(chat, mid, f"❌ {esc(e)}", home_kb())
            return
        self._pgpanel(chat, 0, i)

    def _pgrecur_ask(self, chat: int, mid: int, i: int) -> None:
        from pg import Pg as _Pg
        try:
            p = self._pg_panels()[i]
            tok = _Pg(p["url"], p["username"], p["password"]).login()
            st = _Pg(p["url"], p["username"], p["password"]).settings(tok) \
                if tok else None
            cur = (st or {}).get("webhook", {}).get("recurrent")
        except Exception:
            cur = None
        self.store.put_json(f"pend:{chat}",
                            {"type": "pg_recur", "panel": i}, 600)
        self._show(chat, mid,
                   "⏱ هر چند ثانیه پنل کاربرها را برای هشدار ارزیابی کند؟\n\nفعلی: " +
                   (str(cur) + " ثانیه" if cur is not None else "؟") +
                   "\nپیشنهاد: 3600 (ساعتی).\n\nعدد بین ۶۰ تا ۸۶۴۰۰ بفرست.",
                   [[btn("🔙 انصراف", f"pgpanel:{i}")]])

    def _public_base(self) -> str:
        try:
            with open("/opt/cg-server/config.json", encoding="utf-8") as f:
                import json as _j
                return str(_j.load(f).get("public_url") or "")
        except Exception:
            return ""

    # ---------- Usage monitor ----------

    def _um_home(self, chat: int, mid: int = 0) -> None:
        from monitors import um_cfg
        cfg = um_cfg(self.store)
        txt = "\n".join([
            "🔥 گزارش بد مصرف پاسارگارد", "",
            "کاربرانی که لینکشان پخش شده و بی‌رویه مصرف می‌کنند را پیدا می‌کند.",
            "هر ۶۰ دقیقه از API پنل خوانده و با مصرف ~۱ ساعت و ~۲۴ ساعت قبل مقایسه می‌شود.",
            "الگوی مصرف هر هشدار (پیوسته/جهشی) هم مشخص می‌شود.", "",
            f'وضعیت: {"روشن ✅" if cfg.get("enabled") else "خاموش ❌"}',
            f'⏱ آستانهٔ ساعتی: {cfg.get("hourlyGb")} GB',
            f'📅 آستانهٔ روزانه: {cfg.get("dailyGb")} GB',
            f'🔁 فاصلهٔ تکرار هشدار هر کاربر: {cfg.get("cooldownH")} ساعت',
            f'🧮 حداقل مصرف برای اسنپ‌شات: {cfg.get("minGb")} GB',
            f'📊 گزارش روزانهٔ اخطارشده‌ها: {"روشن ✅" if cfg.get("report") else "خاموش ❌"}',
            "",
            "هشدار فقط برای ادمین ارسال می‌شود. گزارش روزانه فقط کاربرانی را نشان می‌دهد که در همان روز برایشان هشدار فرستاده شده است."])
        kb = [[btn("⏸ خاموش کن" if cfg.get("enabled") else "▶️ روشن کن",
                   "um:toggle")],
              [btn(f'⏱ ساعتی: {cfg.get("hourlyGb")}GB', "um:h"),
               btn(f'📅 روزانه: {cfg.get("dailyGb")}GB', "um:d")],
              [btn(f'🔁 فاصلهٔ هشدار: {cfg.get("cooldownH")}h', "um:c")],
              [btn(f'🧮 حداقل اسنپ‌شات: {cfg.get("minGb")}GB', "um:min"),
               btn("📊 گزارش: روشن" if cfg.get("report")
                   else "📊 گزارش: خاموش", "um:report")],
              [btn("🏠 خانه", "menu")]]
        self._show(chat, mid, txt, kb)

    # ---------- Node monitor ----------

    def _nd_home(self, chat: int, mid: int = 0) -> None:
        from monitors import get_node_monitors
        mons = get_node_monitors(self.store)
        lines = ["📡 مانیتور نود پاسارگارد", "",
                 "هر پنل پاسارگاردی را ثبت کن و برایش «مانیتور نود» بساز؛ "
                 "سپس همین‌جا وضعیت همهٔ نودها را مستقیم می‌بینی. "
                 "اگر نودی قطع یا دوباره وصل شود، فوری خبر داده می‌شود.",
                 "پایش خودکار با فاصلهٔ قابل تنظیم انجام می‌شود.", ""]
        kb = []
        for m in mons:
            st = "⏸" if m.get("enabled") is False else "🟢"
            kb.append([btn(f'{st} {m.get("name") or m.get("id")}',
                           f'ndtg:{m.get("id")}', "plain")])
        kb.append([btn("➕ ساخت مانیتور", "ndmk")])
        if mons:
            kb.append([btn("🗑 حذف مانیتور", "nddelmenu")])
        kb.append([btn("🏠 خانه", "menu")])
        self._show(chat, mid, "\n".join(lines), kb)

    def _nd_mk_interval(self, chat: int, mid: int, i: int) -> None:
        try:
            p = self._pg_panels()[i]
        except IndexError:
            self._show(chat, mid, "❌ پنل پیدا نشد.", home_kb())
            return
        kb = grid3([btn(f"{m} دقیقه", f"ndset:{i}:{m}")
                    for m in (5, 10, 15, 30, 60)])
        kb.append([btn("🔙 بازگشت", "nd"),
                   btn("🏠 خانه", "menu")])
        self._show(chat, mid,
                   f'⏱ فاصله پایش نودهای «{p.get("name")}»؟', kb)

    def _nd_interval_do(self, chat: int, mid: int, arg: str) -> None:
        from monitors import get_node_monitors
        i, mins = arg.split(":")
        try:
            p = self._pg_panels()[int(i)]
            mins = max(1, int(mins))
        except (IndexError, ValueError):
            self._show(chat, mid, "❌", home_kb())
            return
        mons = get_node_monitors(self.store)
        mid_ = f"m{int(__import__('time').time())}"
        mons.append({"id": mid_, "panel_url": p.get("url"),
                     "name": p.get("name") or p.get("url"),
                     "intervalMin": mins, "enabled": True, "excluded": []})
        self.store.put_json("node_monitors", mons)
        self.bot.send(chat,
                      f'✅ مانیتور نود «{p.get("name")}» ساخته شد (هر {mins} دقیقه).')
        self._nd_home(chat, 0)

    def _nd_toggle(self, chat: int, mid: int, mid_: str) -> None:
        from monitors import get_node_monitors
        mons = get_node_monitors(self.store)
        for m in mons:
            if str(m.get("id")) == str(mid_):
                m["enabled"] = not m.get("enabled", True)
        self.store.put_json("node_monitors", mons)
        self._nd_home(chat, mid)

    def _nd_del_ask(self, chat: int, mid: int, mid_: str) -> None:
        self._show(chat, mid, "⚠️ مانیتور حذف شود؟",
                   [[btn("✅ بله", f"nddy:{mid_}"),
                     btn("❌ انصراف", "nd")]])

    def _nd_del_do(self, chat: int, mid: int, mid_: str) -> None:
        from monitors import get_node_monitors
        self.store.put_json("node_monitors",
                            [m for m in get_node_monitors(self.store)
                             if str(m.get("id")) != str(mid_)])
        self.bot.send(chat, "✅ حذف شد.")
        self._nd_home(chat, 0)

    # ---------- Arvan DNS ----------

    def _arvan_acc_list(self) -> list:
        accs = self.store.get_json("arvan_accounts", [])
        return accs if isinstance(accs, list) else []

    def _arvan_home(self, chat: int, mid: int = 0) -> None:
        accs = self._arvan_acc_list()
        names = "، ".join(f'<code>{esc(a.get("name"))}</code>' for a in accs) \
            if accs else "—"
        self._show(chat, mid,
                   "🇮🇷 آروان کلاد\n\n"
                   f"اکانت‌ها: {names}\n\n"
                   "کلید ماشین‌یوزر (npanel → Settings → IAM → Machine users) "
                   "با دسترسی‌های CDN و Cloud Server لازم است.",
                   [[btn("🌐 دامنه‌ها (DNS)", "arvdoms")],
                    [btn("👤 اکانت‌ها", "arvanacc"),
                     btn("🔙 بازگشت", "menu")]])

    def _arvan_accs_page(self, chat=None, mid=None):
        accs = self._arvan_acc_list()
        kb = []
        for i, a in enumerate(accs):
            kb.append([btn(f'👤 {a.get("name")}', "arvanacc",
                           "plain"),
                       btn("🗑", f"arvanaccdel:{i}", "danger")])
        kb.append([btn("➕ افزودن اکانت", "arvanaccadd")])
        kb.append([btn("🔙 بازگشت", "arvan"),
                   btn("🏠 خانه", "menu")])
        self._show(chat, mid,
                   "🇮🇷 آروان — اکانت‌ها\n\nکلید را از پنل آروان "
                   "(Settings → Machine User) بسازید.", kb)

    def _arvan_acc_del_ask(self, chat: int, mid: int, idx: str) -> None:
        try:
            name = self._arvan_acc_list()[int(idx)]["name"]
        except (ValueError, IndexError):
            self._show(chat, mid, "❌ اکانت پیدا نشد.", home_kb())
            return
        self._show(chat, mid, f'⚠️ اکانت «{esc(name)}» حذف شود؟',
                   [[btn("✅ بله، حذف کن", f"arvanaccdelok:{idx}"),
                     btn("❌ انصراف", "arvanacc")]])

    def _arvan_acc_del_do(self, chat: int, mid: int, idx: str) -> None:
        accs = self._arvan_acc_list()
        try:
            accs.pop(int(idx))
        except (ValueError, IndexError):
            self._show(chat, mid, "❌ اکانت پیدا نشد.", home_kb())
            return
        self.store.put_json("arvan_accounts", accs)
        self.bot.send(chat, "✅ اکانت حذف شد.")
        self._arvan_accs_page(chat, 0)

    def _arvan_doms(self, chat: int, mid: int = 0) -> None:
        accs = self._arvan_acc_list()
        if not accs:
            self._show(chat, mid, "📭 اکانتی نیست.",
                       [[btn("➕ افزودن اکانت", "arvanaccadd")],
                        [btn("🔙 بازگشت", "arvan")]])
            return
        kb = []
        for i, a in enumerate(accs):
            try:
                doms = Arvan(a["token"]).domains()
            except ArvanError as e:
                kb.append([btn(f'⚠️ {a.get("name")}: {str(e)[:30]}',
                               "arvanacc", "plain")])
                continue
            for d in doms[:20]:
                dn = d.get("name") or d.get("domain") or "?"
                kb.append([btn(f'🇮🇷 {dn}', f"arvdom:{i}:{dn}")])
        kb.append([btn("🔙 بازگشت", "arvan"),
                   btn("🏠 خانه", "menu")])
        self._show(chat, mid, "🌐 دامنه‌های آروان:", kb)

    def _arvan_recs(self, chat: int, mid: int, acc: int, dom: str,
                    pg: int) -> None:
        try:
            recs = Arvan(self._arvan_acc_list()[acc]["token"]).records(dom)
        except (IndexError, ArvanError) as e:
            self._show(chat, mid, f"❌ {esc(e)}", home_kb())
            return
        self._cf_save_sess(chat, {"acc": f"arv{acc}", "dom": dom,
                                  "results": {
                                      str(r.get("id")): {"acc": acc,
                                                         "dom": dom}
                                      for r in recs[:100]}})
        per, pages = 30, max(1, (len(recs) + 29) // 30)
        pg = max(0, min(pg, pages - 1))
        rows = []
        for r in recs[pg * per:(pg + 1) * per]:
            nm = str(r.get("name") or r.get("host") or "?")
            rows.append(btn(f'{nm}-{r.get("type", "?")}',
                            f'arvrec:{acc}:{dom}:{r.get("id")}'))
        kb = grid3(rows)
        kb.append([btn("➕ افزودن", f"arvaadd:{acc}:{dom}")])
        if pages > 1:
            kb.append([btn("◀️", f"arvdom:{acc}:{dom}") if pg > 0
                       else EMPTY_BTN,
                       btn(f"📄 {pg + 1}/{pages}", "noop"),
                       EMPTY_BTN])
        kb.append([btn("🔙 بازگشت", "arvdoms"),
                   btn("🏠 خانه", "menu")])
        self._show(chat, mid, f"📋 {dom} ({len(recs)} رکورد):", kb)

    def _arvan_rec(self, chat: int, mid: int, acc: int, dom: str,
                   rid: str) -> None:
        try:
            recs = Arvan(self._arvan_acc_list()[acc]["token"]).records(dom)
        except (IndexError, ArvanError) as e:
            self._show(chat, mid, f"❌ {esc(e)}", home_kb())
            return
        r = next((x for x in recs if str(x.get("id")) == str(rid)), None)
        if not r:
            self._show(chat, mid, "❌ رکورد پیدا نشد.", home_kb())
            return
        cloud = r.get("cloud") or r.get("cdn")
        txt = (f'📝 <b>{esc(r.get("name") or r.get("host"))}</b>\n'
               f'نوع: {esc(r.get("type"))}\n'
               f'مقدار: <code>{esc(r.get("value") or r.get("content"))}</code>\n'
               f'ابری: {"روشن" if cloud else "خاموش"}')
        kb = [[btn("✏️ تغییر مقدار", f"arvev:{acc}:{dom}:{rid}"),
               btn("☁️ ابری", f"arvcloud:{acc}:{dom}:{rid}")],
              [btn("🗑 حذف", f"arvdel:{acc}:{dom}:{rid}", "danger")],
              [btn("🔙 بازگشت", f"arvdom:{acc}:{dom}"),
               btn("🏠 خانه", "menu")]]
        self._show(chat, mid, txt, kb)

    def _arvan_del_do(self, chat: int, mid: int, acc: int, dom: str,
                      rid: str) -> None:
        try:
            Arvan(self._arvan_acc_list()[acc]["token"]).call(
                f"/domains/{dom}/dns-records/{rid}", "DELETE")
            self.bot.send(chat, "✅ حذف شد.")
        except (IndexError, ArvanError) as e:
            self._show(chat, mid, f"❌ نشد: {esc(e)}", home_kb())
            return
        self._arvan_recs(chat, 0, acc, dom, 0)

    def _arvan_cloud(self, chat: int, mid: int, acc: int, dom: str,
                     rid: str) -> None:
        try:
            cli = Arvan(self._arvan_acc_list()[acc]["token"])
            recs = cli.records(dom)
            r = next((x for x in recs if str(x.get("id")) == str(rid)), None)
            if not r:
                self._show(chat, mid, "❌ رکورد پیدا نشد.", home_kb())
                return
            cur = bool(r.get("cloud") or r.get("cdn"))
            cli.call(f"/domains/{dom}/dns-records/{rid}", "PUT",
                     {"cloud": not cur})
            self.bot.send(chat,
                          f'✅ ابری {"روشن" if not cur else "خاموش"} شد.')
        except (IndexError, ArvanError) as e:
            self._show(chat, mid, f"❌ نشد: {esc(e)}", home_kb())
            return
        self._arvan_rec(chat, 0, acc, dom, rid)

    # ---------- Auto sub-change (host filter) ----------

    def _hf_home(self, chat: int, mid: int = 0) -> None:
        from hostfilter import get_cfg
        cfg = get_cfg(self.store)
        lines = ["🧭 تعویض خودکار هاست فیلتر", "",
                 "💡 برای بررسی سریع‌تر: هاست‌هایی که نیاز به تعویض ندارند را از «📋 لیست هاست‌ها» با «🚫 استثنا» از چرخه خارج کن؛ هرچه هاست کمتر، ران زودتر تمام می‌شود.",
                 "وضعیت: " + ("▶️ فعال" if cfg.get("enabled") else "⏸ غیرفعال"),
                 "🔌 سرویس بررسی: check-host",
                 "⏱ فاصلهٔ اجرا: هر " + str(cfg.get("intervalMin")) + " دقیقه",
                 "🌆 شهرها: " + "، ".join(cfg.get("citiesSel") or []),
                 "🎯 آستانه: " + str(cfg.get("cities")) + " شهر از " + str(len(cfg.get("citiesSel") or [])) + " (پینگ موفق ≤ " + str(cfg.get("maxOk")) + " از ۴)",
                 "📦 تعداد هر اجرا: " + str(cfg.get("batch")) + " · 🔁 حداکثر تعویض: " + str(cfg.get("maxChanges")),
                 "🔁 بررسی مجدد: " + (str(cfg.get("recheckCount")) + " بار، هر " + str(cfg.get("recheckMin")) + " دقیقه" if cfg.get("recheckCount") else "غیرفعال (تعویض فوری)"),
                 "روش: دامنه‌های هاست‌ها هر " + str(cfg.get("intervalMin")) + " دقیقه از داخل ایران بررسی می‌شوند؛ در صورت فیلتر یک دامنهٔ شماره‌دار جدید ساخته و در همان هاست جایگزین می‌شود."]
        if cfg.get("last_summary"):
            lines.append("")
            lines.append("آخرین ران: " + str(cfg["last_summary"]))
        kb = [[btn("⏸ غیرفعال‌سازی" if cfg.get("enabled") else "▶️ فعال‌سازی", "hftg")],
              [btn("⚡ فورس بررسی کامل", "hfforceall")],
              [btn("📋 لیست هاست‌ها", "hflist")],
              [btn("⚙️ تنظیمات چک‌هاست", "hfsetch")],
              [btn("🏠 خانه", "menu")]]
        self._show(chat, mid, "\n".join(lines), kb)

    def _hf_all_keys(self, pid=None):
        from pg import Pg as _Pg
        keys = []
        panels = self._pg_panels()
        for pi, p in enumerate(panels):
            if pid is not None and pi != pid:
                continue
            try:
                pgc = _Pg(p["url"], p["username"], p["password"])
                hosts = pgc.hosts(pgc.login())
            except Exception:
                continue
            for h in hosts or []:
                if not isinstance(h, dict):
                    continue
                hid = str(h.get("id") or "")
                for f in ("host", "address", "sni"):
                    keys.append(f"{pi}:{hid}:{f}")
        return keys

    def _hf_exc_toggle(self, chat: int, mid: int, pid: str, hid: str,
                       field: str) -> None:
        from hostfilter import get_cfg, save_cfg
        cfg = get_cfg(self.store)
        ex = set(cfg.get("exceptions") or [])
        key = f"{pid}:{hid}:{field}"
        if key in ex:
            ex.discard(key)
        else:
            ex.add(key)
        cfg["exceptions"] = sorted(ex)
        save_cfg(self.store, cfg)
        self._hf_hosts(chat, mid)

    @staticmethod
    def _hf_short(t: str, n: int = 20) -> str:
        t = str(t or "")
        return t if len(t) <= n else t[:n - 1] + "…"

    def _hf_hosts(self, chat: int, mid: int = 0, note: str = "") -> None:
        from hostfilter import get_cfg
        from pg import Pg as _Pg
        cfg = get_cfg(self.store)
        exc = set(cfg.get("exceptions") or [])
        sel = self.store.get_json(f"hfsel:{chat}", {}) or {}
        sel_mode = bool(sel.get("on"))
        sel_set = set(sel.get("sel") or [])
        panels = self._pg_panels()
        lines = ["📋 لیست هاست‌ها (فقط فعال‌ها)", "",
                 "سه ستون: Host | Address | Network — کلیک هر کدام = استثنای همون فیلد (آبی در چرخه · خاکستری مستثنا)",
                 "⚡ برای سرعت بیشتر، هاست‌های اضافه را گروهی استثنا کن 👇", ""]
        if note:
            lines.append(note)
            lines.append("")
        kb = [[btn("🚫 استثنای همه", "hfexcall"),
               btn("✅ لغو استثنای همه", "hfunexcall")]]
        n = 0
        for pi, p in enumerate(panels):
            try:
                pgc = _Pg(p["url"], p["username"], p["password"])
                hosts = pgc.hosts(pgc.login())
            except Exception:
                continue
            lines += ["", f'— 🖥 {p.get("name") or p.get("url")} —']
            kb.append([btn("🚫 همهٔ این پنل", f"hfexcpanel:{pi}"),
                       btn("✅ فعال‌سازی همه", f"hfunexcpanel:{pi}")])
            for h in hosts or []:
                if not isinstance(h, dict):
                    continue
                if h.get("is_disabled"):
                    continue
                n += 1
                hid = str(h.get("id") or "")
                key = f"{pi}:{hid}"
                host_l = str(h.get("remark") or ((h.get("sni") or [""])[0] if isinstance(h.get("sni"), list) else "") or "host")[:22]
                addr_l = str(((h.get("address") or [""])[0] if isinstance(h.get("address"), list) else h.get("address")) or "—")[:26]
                sni_v = h.get("sni") if isinstance(h.get("sni"), list) else []
                host_v = h.get("host") if isinstance(h.get("host"), list) else []
                net_l = str((sni_v + host_v + ["—"])[0])[:26]
                lines.append(f"{n}) {host_l} | {addr_l} | {net_l}")
                if sel_mode:
                    picked = key in sel_set
                    kb.append([btn(f'{"✅ " if picked else "⬜ "}{self._hf_short(host_l)}',
                                   f"hfgs:{pi}:{hid}",
                                   "primary" if picked else "plain"),
                               btn(f'{"✅ " if picked else "⬜ "}{self._hf_short(addr_l)}',
                                   f"hfgs:{pi}:{hid}",
                                   "primary" if picked else "plain"),
                               btn(f'{"✅ " if picked else "⬜ "}{self._hf_short(net_l)}',
                                   f"hfgs:{pi}:{hid}",
                                   "primary" if picked else "plain")])
                else:
                    eH = key + ":host" in exc
                    eA = key + ":address" in exc
                    eN = key + ":sni" in exc
                    kb.append([btn(f'{"⬜ " if eH else "✅ "}{self._hf_short(host_l)}',
                                   f"hfexc:{pi}:{hid}:host",
                                   "plain" if eH else "primary"),
                               btn(f'{"⬜ " if eA else "✅ "}{self._hf_short(addr_l)}',
                                   f"hfexc:{pi}:{hid}:address",
                                   "plain" if eA else "primary"),
                               btn(f'{"⬜ " if eN else "✅ "}{self._hf_short(net_l)}',
                                   f"hfexc:{pi}:{hid}:sni",
                                   "plain" if eN else "primary")])
        if not n:
            lines.append("📭 هاستی پیدا نشد.")
        if sel_mode:
            lines.append(f"🗂 {len(sel_set)} انتخاب شده:")
            kb.append([btn("🚫 استثنا برای انتخاب‌ها", "hfgexc"),
                       btn("✅ لغو استثنا", "hfgunexc")])
            kb.append([btn("✅ انتخاب همه", "hfgall"),
                       btn("🧹 پاک‌سازی", "hfgclr")])
            kb.append([btn("❌ خروج از انتخاب", "hfgdone")])
        else:
            kb.append([btn("🗂 انتخاب گروهی", "hfgmode")])
        kb.append([btn("📜 تاریخچه", "hfhist")])
        kb.append([btn("🔙 بازگشت", "hf"),
                   btn("🏠 خانه", "menu")])
        self._show(chat, mid, "\n".join(lines)[:3500], kb)

    def _hf_hist(self, chat: int, mid: int = 0) -> None:
        log = self.store.get_json("host_filter_log", []) or []
        lines = ["📜 تاریخچه تعویض‌ها", ""]
        for e in log[-15:]:
            ts = str(e.get("ts", ""))[:16].replace("T", " ")
            if e.get("kind") == "swap":
                lines.append(f'{ts} · {e.get("from")} → {e.get("to")}')
            else:
                lines.append(f'{ts} · {e.get("kind")}')
        if len(log) <= 1:
            lines.append("📭 چیزی ثبت نشده.")
        self._show(chat, mid, "\n".join(lines),
                   [[btn("🔙 بازگشت", "hflist"),
                     btn("🏠 خانه", "menu")]])


    def _hf_settings(self, chat: int, mid: int = 0) -> None:
        from hostfilter import get_cfg
        cfg = get_cfg(self.store)
        lines = ["⚙️ تنظیمات چک‌هاست", "",
                 "⚠️ هرچه فاصلهٔ اجرا کوتاه‌تر یا تعداد هر اجرا بیشتر باشد، درخواست‌های بیشتری به کلادفلر فرستاده می‌شود و ممکن است باعث محدود شدن شود.", "",
                 "• ⏱ فاصلهٔ اجرا: " + str(cfg.get("intervalMin")) + " دقیقه",
                 "• 🌆 شهرهای بررسی‌شده: " + "، ".join(cfg.get("citiesSel") or []),
                 "• 🎯 حداقل شهرهای فیلتر: " + str(cfg.get("cities")),
                 "• 📶 حداکثر پینگ موفق مجاز: " + str(cfg.get("maxOk")) + " از ۴",
                 "• 🔁 تعداد بررسی مجدد: " + str(cfg.get("recheckCount")),
                 "• ⏱ فاصلهٔ بررسی مجدد: هر " + str(cfg.get("recheckMin")) + " دقیقه",
                 "• 📦 تعداد بررسی در هر اجرا: " + str(cfg.get("batch")),
                 "• 🔁 حداکثر تعویض در هر اجرا: " + str(cfg.get("maxChanges"))]
        kb = [[btn("⏱ فاصلهٔ اجرا", "hfnum:intervalMin")],
              [btn("🎯 حداقل شهر", "hfnum:cities"),
               btn("📶 حداکثر پینگ موفق", "hfnum:maxOk")],
              [btn("📦 تعداد هر اجرا", "hfnum:batch"),
               btn("🔁 حداکثر تعویض", "hfnum:maxChanges")],
              [btn("♻️ تعداد بررسی مجدد", "hfnum:recheckCount"),
               btn("⏱ فاصلهٔ بررسی مجدد", "hfnum:recheckMin")],
              [btn("🔙 بازگشت", "hf"),
               btn("🏠 خانه", "menu")]]
        self._show(chat, mid, "\n".join(lines), kb)

    def _hf_run_bg(self, chat: int, force: bool, only: str) -> None:
        import threading as _th
        from hostfilter import run as _run
        from pg import Pg as _Pg
        bot, store = self.bot, self.store
        panels = self._pg_panels()
        accs = self._cf_accs()
        admin = self.admin_id
        def _go():
            try:
                r = _run(bot, store, panels, admin, accs,
                         lambda p: _Pg(p["url"], p["username"], p["password"]),
                         force=force, only=only)
                try:
                    bot.send(chat, f'✅ ران تمام شد: بررسی {r.get("checked", 0)} دامنه — {r.get("changed", 0)} تعویض.')
                except Exception:
                    pass
            except Exception:
                pass
        _th.Thread(target=_go, daemon=True).start()

    # ---------- Servers (SSH) ----------

    def _srv_list(self) -> list:
        lst = self.store.get_json("srv_list", [])
        return lst if isinstance(lst, list) else []

    def _srv_home(self, chat: int, mid: int = 0) -> None:
        lst = self._srv_list()
        lines = ["🖥 سرورها و مانیتورینگ", "",
                 "مدیریت سرورهای لینوکسی از طریق SSH:\n"
                 "• مانیتور CPU / RAM / دیسک / پهنای باند\n", ""]
        kb = []
        if lst:
            lines.append(f"🖧 سرورهای ثبت‌شده ({len(lst)}): روی هرکدام بزنید:")
            rows = [btn(f'🖧 {s.get("name")}', f'srvopen:{s.get("id")}',
                        "plain") for s in lst]
            kb.extend(grid2(rows))
        kb.append([btn("📥 افزودن گروهی", "srvaddbulk"),
                   btn("➕ افزودن سرور", "srvadd")])
        kb.append([btn("➕ افزودن از ساب‌دامنه", "srvsub"),
                   btn("📥 وارد کردن از دیتاسنترها", "srvimport")])
        if lst:
            kb.append([btn("🗑 حذف سرور", "srvdel")])
        kb.append([btn("📊 مانیتور سرورها", "srvmon"),
                   btn("🔑 رمزهای ذخیره‌شده", "srvpw")])
        kb.append([btn("🏠 خانه", "menu")])
        self._show(chat, mid, "\n".join(lines), kb)

    def _srv_get(self, sid: str):
        for i, s in enumerate(self._srv_list()):
            if str(s.get("id")) == str(sid):
                return i, s
        return None, None

    def _srv_open(self, chat: int, mid: int, sid: str) -> None:
        i, s = self._srv_get(sid)
        if not s:
            self._show(chat, mid, "❌ سرور پیدا نشد.",
                       [[btn("🖥 سرورها و مانیتورینگ", "srv")]])
            return
        lines = [f'🖧 {esc(s.get("name"))}',
                 f'🌐 <code>{esc(s.get("host"))}:{esc(s.get("port") or 22)}</code>  ({esc(s.get("user") or "root")})',
                 f'🔐 {"🔑 کلید SSH" if s.get("auth") == "key" else "🔒 رمز عبور"}']
        if s.get("note"):
            lines.append(f'📝 {esc(s.get("note"))}')
        lines += ["", "یک عملیات را انتخاب کنید:"]
        kb = [[btn("📊 مانیتور سرور", f"srvstats:{i}"),
               btn("⚙️ آستانه‌ها", "srvmon")],
              [btn("🔄 ریبوت", f"srvreboot:{s.get('id')}"),
               btn("⏱ آپدیت و آپگرید", f"srvupd:{s.get('id')}")],
              [btn("ℹ️ مشخصات سیستم", f"srvinfo:{s.get('id')}"),
               btn("💾 فضای دیسک", f"srvdisk:{s.get('id')}")],
              [btn("✏️ ویرایش", f"srvedit:{s.get('id')}"),
               btn("🗑 حذف", f'srvdelx:{s.get("id")}', "danger")],
              [btn("🔙 بازگشت", "srv"),
               btn("🏠 خانه", "menu")]]
        self._show(chat, mid, "\n".join(lines), kb)

    def _srv_ssh(self, s: dict, cmd: str):
        from sshop import run_cmd
        return run_cmd(s.get("host"), s.get("user") or "root",
                       s.get("secret") or "", s.get("port") or 22, cmd)

    def _srv_info(self, chat: int, mid: int, sid: str) -> None:
        _, s = self._srv_get(sid)
        if not s:
            self._show(chat, mid, "❌ سرور پیدا نشد.", home_kb())
            return
        self._show(chat, mid, "⏳ در حال خواندن…", home_kb())
        from sshop import INFO_CMD
        r = self._srv_ssh(s, INFO_CMD)
        if not r["ok"]:
            self._show(chat, mid, f'❌ وصل نشد: <code>{esc(r["err"])}</code>',
                       [[btn("🔙 بازگشت", "srv")]])
            return
        out = []
        for ln in r["out"].splitlines():
            if ":" in ln:
                k, v = ln.split(":", 1)
                out.append(f"{k.strip()}: <code>{esc(v.strip())}</code>")
        self._show(chat, mid, f'🖥 {esc(s.get("name"))}\n\n' + "\n".join(out),
                   [[btn("🔙 بازگشت", f"srvopen:{sid}"),
                     btn("🏠 خانه", "menu")]])

    def _srv_disk(self, chat: int, mid: int, sid: str) -> None:
        _, s = self._srv_get(sid)
        if not s:
            self._show(chat, mid, "❌ سرور پیدا نشد.", home_kb())
            return
        from sshop import DISK_CMD
        r = self._srv_ssh(s, DISK_CMD)
        if not r["ok"]:
            self._show(chat, mid, f'❌ وصل نشد: <code>{esc(r["err"])}</code>',
                       [[btn("🔙 بازگشت", "srv")]])
            return
        self._show(chat, mid,
                   f'💾 دیسک «{esc(s.get("name"))}»:\n<code>{esc(r["out"])}</code>',
                   [[btn("🔙 بازگشت", f"srvopen:{sid}"),
                     btn("🏠 خانه", "menu")]])

    def _srv_reboot(self, chat: int, mid: int, sid: str) -> None:
        _, s = self._srv_get(sid)
        if not s:
            self._show(chat, mid, "❌ سرور پیدا نشد.", home_kb())
            return
        r = self._srv_ssh(s, "reboot")
        self.bot.send(chat, "✅ دستور ریبوت ارسال شد."
                      if r["ok"] else f'❌ نشد: <code>{esc(r["err"])}</code>')
        self._srv_open(chat, 0, sid)

    def _srv_del_ask(self, chat: int, mid: int, sid: str) -> None:
        self._show(chat, mid, "⚠️ سرور از لیست حذف شود؟",
                   [[btn("✅ بله", f"srvdely:{sid}"),
                     btn("❌ انصراف", "srv")]])

    def _srv_del_do(self, chat: int, mid: int, sid: str) -> None:
        self.store.put_json("srv_list",
                            [s for s in self._srv_list()
                             if str(s.get("id")) != str(sid)])
        self.bot.send(chat, "✅ حذف شد.")
        self._srv_home(chat, 0)

    def _srvmon_run(self, chat: int) -> None:
        from monitors import run_srvmon
        from sshop import stats as _stats
        lst = self._srv_list()
        n = run_srvmon(self.bot, self.store, lst, chat,
                       lambda s: _stats(s.get("host"), s.get("user") or "root",
                                        s.get("secret") or "",
                                        s.get("port") or 22),
                       manual=True)
        if n == 0:
            self.bot.send(chat, "✅ بررسی شد؛ هشدار تازه‌ای نیست.")

    def _srvstats(self, chat: int, mid: int, idx: str) -> None:
        try:
            s = self._srv_list()[int(idx)]
        except (ValueError, IndexError):
            self._show(chat, mid, "❌ سرور پیدا نشد.",
                       [[btn("🖥 سرورها و مانیتورینگ", "srv")]])
            return
        self._show(chat, mid, "⏳ در حال جمع‌آوری آمار…", home_kb())
        from sshop import stats as _stats
        from monitors import _fmt_gb
        st = _stats(s.get("host"), s.get("user") or "root",
                    s.get("secret") or "", s.get("port") or 22)
        if st.get("error"):
            self._show(chat, mid,
                       f'❌ وصل نشد: <code>{esc(st["error"][:200])}</code>',
                       [[btn("🔄 بروزرسانی", f"srvstats:{idx}"),
                         btn("⚙️ آستانه‌ها", "srvmon")],
                        [btn("🔙 بازگشت", f'srvopen:{s.get("id")}'),
                         btn("🏠 خانه", "menu")]])
            return
        up_d = (st.get("uptimeS") or 0) // 86400
        up_h = ((st.get("uptimeS") or 0) % 86400) // 3600
        mem, disk, cpu = st.get("mem") or {}, st.get("disk") or {}, \
            st.get("cpu") or {}
        short_ip = str(s.get("host") or "")[:15]
        from monitors import _fmt_gb
        txt = (f'📊 {s.get("name")}\n⏱ {up_d} روز و {up_h} ساعت')
        kb = [[btn(f"🌐 {short_ip}", "noop", "plain"),
               btn(f'⚙️ {cpu.get("pct", 0)}%', "noop", "plain"),
               btn(f'🧠 {mem.get("pct", 0)}%', "noop", "plain"),
               btn(f'🗄 {disk.get("pct", "—")}', "noop", "plain")],
              [btn(f'⚙️ {cpu.get("cores", "?")} هسته', "noop", "plain"),
               btn(f'🧠 {_fmt_gb(mem.get("usedMb"))}/{_fmt_gb(mem.get("totalMb"))}',
                   "noop", "plain"),
               btn(f'🗄 {disk.get("used", "—")}/{disk.get("total", "—")}',
                   "noop", "plain"),
               btn(f"⏱ {up_d}r {up_h}h", "noop", "plain")],
              [btn("🔄 بروزرسانی", f"srvstats:{idx}"),
               btn("⚙️ آستانه‌ها", "srvmon")],
              [btn("🔙 بازگشت", f'srvopen:{s.get("id")}'),
               btn("🏠 خانه", "menu")]]
        self._show(chat, mid, txt, kb)

    # ---------- Traffic (subs DNS queries) ----------

    TRAF_PAGE = 20

    @staticmethod
    def _traf_range_label(d: int) -> str:
        return ("۱ روز گذشته" if d == 1 else
                "۱ ماه گذشته" if d == 30 else "۷ روز گذشته")

    def _traf_zones(self, chat: int, mid: int = 0, flt: str = "all",
                    pg: int = 0) -> None:
        accs = self._cf_accs()
        allz = self._cf_all_zones()
        if not allz:
            self._show(chat, mid, "📊 ترافیک ساب‌ها\n\n📭 دامنه‌ای پیدا نشد.",
                       [[btn("🔙 بازگشت", "dommon"),
                         btn("🏠 خانه", "menu")]])
            return
        zones = allz if flt == "all" else \
            [z for z in allz if z["_acc"] == int(flt)]
        pages = max(1, (len(zones) + self.TRAF_PAGE - 1) // self.TRAF_PAGE)
        pg = max(0, min(pg, pages - 1))
        rows = [btn(f'📊 {z["name"]}', f'traz:{z["_acc"]}:{z["id"]}:0',
                    "plain")
                for z in zones[pg * self.TRAF_PAGE:(pg + 1) * self.TRAF_PAGE]]
        kb = grid2(rows)
        kb.append([btn("📁 ✅ همه اکانت‌ها" if flt == "all"
                       else "📁 همه اکانت‌ها", "trazf:all:0")])
        if len(accs) > 1:
            kb.extend(grid3([
                btn(f'{"👤 ✅ " if flt == str(j) else "👤 "}{a["name"]}',
                    f"trazf:{j}:0") for j, a in enumerate(accs)]))
        if pages > 1:
            nav = []
            nav.append(btn("⬅️", f"trazf:{flt}:{pg - 1}") if pg > 0
                       else EMPTY_BTN)
            nav.append(btn("🔙 بازگشت", "dommon"))
            nav.append(btn("➡️", f"trazf:{flt}:{pg + 1}")
                       if pg < pages - 1 else EMPTY_BTN)
            kb.append(nav)
        else:
            kb.append([btn("🔙 بازگشت", "dommon"), btn("🏠 خانه", "menu")])
        label = "همه" if flt == "all" else \
            (accs[int(flt)]["name"] if flt.isdigit()
             and int(flt) < len(accs) else "؟")
        title = f"📊 ترافیک ساب‌ها — {label}"
        if pages > 1:
            title += f" — صفحه {pg + 1} از {pages}"
        title += "\n\nترافیک ۷ روزهٔ DNS را ببینید — روی دامنه کلیک کنید."
        self._show(chat, mid, title, kb)

    def _traf_zone(self, chat: int, mid: int, acc: int, zid: str,
                   pg: int = 0) -> None:
        from cfusage import fetch_traffic_counts
        accs = self._cf_accs()
        if acc < 0 or acc >= len(accs):
            self._show(chat, mid, "❌ اکانت پیدا نشد.",
                       [[btn("🔙 بازگشت", "traf"),
                         btn("🏠 خانه", "menu")]])
            return
        token = accs[acc]["token"]
        try:
            zones = Cf(token).zones() or []
        except CfError as e:
            self._show(chat, mid, f"❌ خطای کلادفلر: {esc(e)}",
                       [[btn("🔙 بازگشت", "traf")]])
            return
        zone = next((z for z in zones if z.get("id") == zid), None)
        if not zone:
            self._show(chat, mid, "❌ دامنه پیدا نشد.",
                       [[btn("🔙 بازگشت", "traf"),
                         btn("🏠 خانه", "menu")]])
            return
        t = fetch_traffic_counts(token, zid)
        if t.get("need_perm") or t.get("error"):
            body = ("❌ دسترسی Analytics نیست.\n\n۱) به توکن این دسترسی را اضافه کن:\n"
                    "<code>Zone → Analytics → Read</code>\n\n"
                    "۲) چون توکن قابل ویرایش نیست، توکن جدید بساز و توی ربات جایگزین کن:\n"
                    "کلودفلر ← 👤 اکانت‌ها ← حذف قدیمی + افزودن جدید"
                    if t.get("need_perm") else
                    f'❌ خطا در آمار:\n{esc(t.get("error"))}')
            self._show(chat, mid, f'📊 ترافیک {esc(zone["name"])}\n\n{body}',
                       [[btn("🔙 بازگشت", "traf"), btn("🏠 خانه", "menu")]])
            return
        counts = t.get("counts") or {}
        try:
            recs = Cf(token).records(zid) or []
        except CfError as e:
            self._show(chat, mid, f"❌ خطای کلادفلر: {esc(e)}",
                       [[btn("🔙 بازگشت", "traf")]])
            return
        zname = str(zone["name"])
        rows = [{"r": r, "n": counts.get(str(r.get("name") or "").lower(), 0)}
                for r in recs
                if str(r.get("type") or "").upper()
                in ("A", "AAAA", "CNAME")]
        rows.sort(key=lambda x: (-x["n"], str(x["r"].get("name") or "")))
        pages = max(1, (len(rows) + self.TRAF_PAGE - 1) // self.TRAF_PAGE)
        pg = max(0, min(pg, pages - 1))
        active = sum(1 for x in rows if x["n"] > 0)
        zeros = len(rows) - active
        lines = [f'📊 ترافیک ۷ روز گذشته — {esc(zname)}',
                 f'🧮 {len(rows)} ساب | 🟢 {active} فعال | ⚪ {zeros} صفر', ""]
        for x in rows[pg * self.TRAF_PAGE:(pg + 1) * self.TRAF_PAGE]:
            r = x["r"]
            n = r.get("name") or ""
            short = "@" if n == zname else (n[:-(len(zname) + 1)]
                                            if n.endswith("." + zname) else n)
            lines.append(f'{"🟢" if x["n"] > 0 else "⚪"} {esc(short)} '
                         f'({esc(r.get("type"))}) — '
                         f'{fa_d(format(x["n"], ","))} کوئری')
        if pages > 1:
            lines += ["", f"صفحه {pg + 1} از {pages}"]
        lines += ["", "⚠️ ساب کم‌استفاده هم صفر نشون میده؛ قبل از حذف مطمئن شو."]
        kb = []
        if pg > 0 and pg < pages - 1:
            kb.append([btn("⬅️ قبلی", f"traz:{acc}:{zid}:{pg - 1}"),
                       btn("➡️ بعدی", f"traz:{acc}:{zid}:{pg + 1}")])
        elif pg > 0:
            kb.append([btn("⬅️ قبلی", f"traz:{acc}:{zid}:{pg - 1}")])
        elif pg < pages - 1:
            kb.append([btn("➡️ بعدی", f"traz:{acc}:{zid}:{pg + 1}")])
        if zeros > 0:
            kb.append([btn(f"🗑 حذف {zeros} ساب صفر",
                           f"trazdel:{acc}:{zid}", "danger")])
        kb.append([btn("🔙 بازگشت", "traf"), btn("🏠 خانه", "menu")])
        self._show(chat, mid, "\n".join(lines)[:3500], kb)

    def _traf_zero_confirm(self, chat: int, mid: int, acc: int,
                           zid: str) -> None:
        accs = self._cf_accs()
        zname = zid
        try:
            for z in Cf(accs[acc]["token"]).zones() or []:
                if z.get("id") == zid:
                    zname = z.get("name")
                    break
        except (CfError, IndexError):
            pass
        self._show(chat, mid,
                   f'🗑 همهٔ ساب‌های {esc(zname)} با ترافیک صفر حذف شوند؟\n\n'
                   "⚠️ ساب کم‌استفاده هم صفر حساب می‌شود؛ مطمئنی؟",
                   [[btn("✅ بله، حذف کن", f"trazdely:{acc}:{zid}",
                         "danger"),
                     btn("❌ انصراف", f"traz:{acc}:{zid}:0")]])

    def _traf_zero_do(self, chat: int, mid: int, acc: int, zid: str) -> None:
        from cfusage import fetch_traffic_counts
        accs = self._cf_accs()
        token = accs[acc]["token"]
        t = fetch_traffic_counts(token, zid)
        if t.get("need_perm") or t.get("error"):
            self._show(chat, mid, "❌ خطا در آمار.",
                       [[btn("🔙 بازگشت", "traf"),
                         btn("🏠 خانه", "menu")]])
            return
        counts = t.get("counts") or {}
        client = Cf(token)
        recs = client.records(zid) or []
        zeros = [r for r in recs
                 if str(r.get("type") or "").upper()
                 in ("A", "AAAA", "CNAME")
                 and not counts.get(str(r.get("name") or "").lower())]
        ok = 0
        for r in zeros:
            try:
                client.del_record(zid, r.get("id"))
                ok += 1
            except CfError:
                continue
        self._show(chat, mid,
                   f"✅ {ok} از {len(zeros)} رکورد بدون ترافیک حذف شد.",
                   [[btn("📊 ترافیک", "traf")]])

    # ---------- Domain expiry monitor ----------

    def _domexp_list(self) -> list:
        lst = self.store.get_json("dom_expiry", [])
        return lst if isinstance(lst, list) else []

    def _domexp_cfg(self) -> dict:
        c = self.store.get_json("dom_expiry_cfg", {}) or {}
        return {"provider": c.get("provider")
                or "whoisxmlapi_domaininfo",
                "key": c.get("key") if isinstance(c.get("key"), str) else ""}

    def _domexp_home(self, chat: int, mid: int = 0, page: int = 0) -> None:
        import domexpiry as de
        now = de._now_ms()
        lst = self._domexp_list()
        srt = sorted(lst, key=lambda x: x.get("expiry")
                     if isinstance(x.get("expiry"), (int, float))
                     else 8640000000000000)
        cfg = self._domexp_cfg()
        text = "🗓 مانیتور انقضای دامنه\n\n"
        text += ("ℹ️ دامنه‌های بین‌المللی از RDAP و دامنه‌های .ir از whois.nic.ir استعلام می‌شوند.\n")
        text += ("🔑 کلید API ثبت شده؛ برای دامنه‌های .ir که ایرنیک مخفی کرده، از دیتابیس تاریخی (WhoisXMLAPI) استعلام می‌شود.\n\n"
                 if cfg["key"] else
                 "⚠️ ایرنیک تاریخ دامنه‌های .ir را حذف/مخفی کرده. برای استعلام خودکار، «🔑 ثبت کلید API» را بزن؛ وگرنه از «✍️ ورود دستی» تاریخ را ثبت کن.\n\n")
        if not srt:
            text += ("📭 دامنه‌ای ثبت نشده. از دکمه‌های زیر اضافه کن:\n"
                     "• «🌍 افزودن از اکانت‌ها»: همهٔ دامنه‌های اکانت‌های کلودفلر یک‌جا اضافه می‌شوند.\n"
                     "• «✍️ ورود دستی»: یک دامنه را با نام یا با تاریخ دستی ثبت کن.")
        else:
            text += f"📌 {len(srt)} دامنه (مرتب بر اساس نزدیک‌ترین انقضا):\n\n"
            per = 30
            pages = max(1, (len(srt) + per - 1) // per)
            p = max(0, min(page, pages - 1))
            for it in srt[p * per:(p + 1) * per]:
                text += de.line(it, now) + "\n"
                if isinstance(it.get("expiry"), (int, float)):
                    text += f'   🗓 انقضا: {fmt_jalali(it["expiry"]).split(" - ")[0]}\n'
            if pages > 1:
                text += f"\nصفحه {p + 1} از {pages}"
        kb = [[btn("🌍 افزودن از اکانت‌ها", "domexpall"),
               btn("✍️ ورود دستی", "domexpa")]]
        if srt:
            kb.append([btn("🗓 ثبت تاریخ دستی", "domexpcal"),
                       btn("🗑 حذف دامنه", "domexpd")])
        kb.append([btn("🔄 استعلام دوبارهٔ همه", "domexp")])
        kb.append([btn("🔑 کلید API (ثبت‌شده ✅)" if cfg["key"]
                       else "🔑 ثبت کلید API", "domexpkey")])
        kb.append([btn("🔙 بازگشت", "dommon"), btn("🏠 خانه", "menu")])
        self._show(chat, mid, text[:4000], kb)

    def _domexp_refresh(self, chat: int, mid: int) -> None:
        self._show(chat, mid, "⏳ در حال استعلام دامنه‌ها…")
        self._domexp_check_all()
        self._domexp_home(chat, mid, 0)

    def _domexp_check_all(self, batch: int = 8) -> list:
        import domexpiry as de
        lst = self._domexp_list()
        cfg = self._domexp_cfg()
        key = cfg["key"]
        for it in lst:
            if it.get("manual") and isinstance(it.get("expiry"), (int, float)):
                continue
            r = de.fetch_domain_expiry(it.get("domain"), key)
            if r.get("status") == "ok":
                it["status"] = "ok"
                it["expiry"] = r.get("expiry")
            else:
                it["status"] = r.get("status")
                it["expiry"] = None
        self.store.put_json("dom_expiry", lst)
        return lst

    def _domexp_add_all(self, chat: int, mid: int) -> None:
        self._show(chat, mid, "⏳ در حال خواندن دامنه‌ها از همهٔ اکانت‌ها…")
        import re as _re
        names, seen = [], set()
        for z in self._cf_all_zones():
            d = str(z.get("name") or "").lower().strip()
            if d and _re.match(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$", d) \
                    and d not in seen:
                seen.add(d)
                names.append(d)
        if not names:
            self._show(chat, mid,
                       "📭 دامنه‌ای در اکانت‌های کلودفلر پیدا نشد.",
                       [[btn("🔙 بازگشت", "domexp"),
                         btn("🏠 خانه", "menu")]])
            return
        lst = self._domexp_list()
        have = {x.get("domain") for x in lst}
        added = 0
        import time as _t
        for d in names:
            if d in have:
                continue
            lst.append({"domain": d, "status": "pending", "expiry": None,
                        "added": int(_t.time() * 1000)})
            added += 1
        self.store.put_json("dom_expiry", lst)
        if not added:
            self._show(chat, mid,
                       f"✅ همهٔ {len(names)} دامنه از قبل در مانیتور بودند.",
                       [[btn("🔙 بازگشت", "domexp"),
                         btn("🏠 خانه", "menu")]])
            return
        self.bot.send(chat, f"⏳ {added} دامنهٔ جدید اضافه شد. در حال استعلام…")
        self._domexp_check_all()
        self._domexp_home(chat, 0, 0)

    def _domexp_cal_list(self, chat: int, mid: int) -> None:
        lst = self._domexp_list()
        if not lst:
            self._show(chat, mid, "📭 دامنه‌ای ثبت نشده.",
                       [[btn("🔙 بازگشت", "domexpp:0"),
                         btn("🏠 خانه", "menu")]])
            return
        hidden = [x for x in lst if x.get("status") != "ok"]
        show = hidden or lst
        kb = [[btn(f'🗓 {x.get("domain")}',
                   "domexpi:" + urllib.parse.quote(str(x.get("domain"))))]
              for x in show[:30]]
        kb.append([btn("🔙 بازگشت", "domexpp:0"), btn("🏠 خانه", "menu")])
        self._show(chat, mid,
                   "🗓 برای کدام دامنه تاریخ انقضا را دستی ثبت/اصلاح کنیم؟",
                   kb)

    def _domexp_cal_domain(self, chat: int, mid: int, dom: str) -> None:
        item = next((x for x in self._domexp_list()
                     if x.get("domain") == dom), None)
        if not item:
            self._show(chat, mid, "❌ دامنه پیدا نشد.",
                       [[btn("🔙 بازگشت", "domexpp:0"),
                         btn("🏠 خانه", "menu")]])
            return
        self.store.put_json(f"pend:{chat}",
                            {"type": "domexp_date", "domain": dom}, 900)
        cur = (f'\n\n🗓 فعلی: {fmt_jalali(item["expiry"]).split(" - ")[0]}'
               if isinstance(item.get("expiry"), (int, float)) else "")
        self._show(chat, mid,
                   f'🗓 تاریخ انقضای <code>{esc(dom)}</code> را به شمسی بفرستید:\n'
                   "مثال: 1405/06/20\n\n"
                   "ℹ️ بعد از ثبت، تاریخ دستی مرجع است و دیگر استعلام خودکار روی آن انجام نمی‌شود."
                   + cur,
                   [[btn("🔙 انصراف", "domexpp:0")]])

    def _domexp_del_list(self, chat: int, mid: int) -> None:
        lst = self._domexp_list()
        if not lst:
            self._show(chat, mid, "📭 دامنه‌ای ثبت نشده.",
                       [[btn("🔙 بازگشت", "domexpp:0"),
                         btn("🏠 خانه", "menu")]])
            return
        kb = [[btn(f'🗑 {x.get("domain")}', f"domexpdy:{i}")]
              for i, x in enumerate(lst[:40])]
        kb.append([btn("🔙 بازگشت", "domexpp:0"), btn("🏠 خانه", "menu")])
        self._show(chat, mid, "🗑 کدام دامنه از مانیتور حذف شود؟", kb)

    def _domexp_del(self, chat: int, mid: int, idx: str) -> None:
        lst = self._domexp_list()
        try:
            rm = lst.pop(int(idx))
        except (ValueError, IndexError):
            self._show(chat, mid, "❌ دامنه پیدا نشد.", home_kb())
            return
        self.store.put_json("dom_expiry", lst)
        self._show(chat, mid,
                   f'✅ «{esc(rm.get("domain"))}» از مانیتور حذف شد.',
                   [[btn("🗓 مانیتور انقضای دامنه", "domexpp:0")]])

    def _domexp_key_page(self, chat: int, mid: int) -> None:
        cfg = self._domexp_cfg()
        has = bool(cfg["key"])
        masked = (cfg["key"][:4] + "…" + cfg["key"][-4:]) if has else "—"
        kb = [[btn("♻️ تغییر کلید API" if has else "🔑 ثبت کلید API",
                   "domexpk")]]
        if has:
            kb.append([btn("🧪 تست روی pcapps.ir", "domexptest")])
            kb.append([btn("🗑 حذف کلید", "domexpkdel")])
        kb.append([btn("🔙 بازگشت", "domexpp:0"), btn("🏠 خانه", "menu")])
        self._show(chat, mid,
                   "🔑 کلید API انقضای دامنه\n\n"
                   f'وضعیت: {"ثبت‌شده ✅" if has else "ثبت‌نشده ❌"}\n'
                   f'کلید: <code>{esc(masked)}</code>\n'
                   "سرویس: WhoisXMLAPI — Domain Info API\n\n"
                   "چرا لازم است؟ ایرنیک تاریخ انقضای دامنه‌های .ir را از whois حذف کرده؛ این API فیلدهای حذف‌شده را از دیتابیس تاریخی پر می‌کند.\n\n"
                   "برای گرفتن کلید (۵۰۰ کردیت رایگان، بدون کارت):\n"
                   "domain-info.whoisxmlapi.com/api/signup\n\n"
                   "کلید فقط در همین ربات ذخیره می‌شود.",
                   kb)

    def _domexp_test(self, chat: int, mid: int) -> None:
        import domexpiry as de
        cfg = self._domexp_cfg()
        if not cfg["key"]:
            self._show(chat, mid, "❌ کلیدی ثبت نشده.",
                       [[btn("🔙 بازگشت", "domexpkey"),
                         btn("🏠 خانه", "menu")]])
            return
        self._show(chat, mid, "⏳ در حال تست کلید روی pcapps.ir…")
        r = de.whois_xml_domain_info("pcapps.ir", cfg["key"])
        if r.get("status") == "ok":
            self._show(chat, mid,
                       "✅ کلید معتبر است.\n"
                       f'🗓 انقضای pcapps.ir: {fmt_jalali(r["expiry"]).split(" - ")[0]}\n'
                       f'⏳ باقی‌مانده: {de.remaining(r["expiry"])}',
                       [[btn("🔙 بازگشت", "domexpkey"),
                         btn("🏠 خانه", "menu")]])
        elif r.get("status") == "api_error":
            self._show(chat, mid,
                       f'⚠️ تست ناموفق بود:\n<code>{esc(r.get("message"))}</code>',
                       [[btn("🔙 بازگشت", "domexpkey")]])
        else:
            self._show(chat, mid,
                       "⚠️ برای pcapps.ir تاریخی برنگشت (ممکن است در دیتابیس تاریخی هم نباشد).",
                       [[btn("🔙 بازگشت", "domexpkey")]])

    def _sslm_home(self, chat: int, mid: int = 0) -> None:
        from sslmon import get_monitors, check_all, result_line
        mons = get_monitors(self.store)
        lines = ["🔐 مانیتور گواهی SSL", ""]
        if not mons:
            lines.append("📭 دامنه‌ای برای نظارت ثبت نشده.\nبرای هشدار هنگام نزدیک شدن به انقضا (پیش‌فرض ۵ روز)، یک دامنه اضافه کنید.")
        else:
            try:
                lines += [result_line(it) for it in check_all(self.store)]
            except Exception as e:
                lines.append(f"❌ خطا در بررسی: {esc(e)[:100]}")
        kb = [[btn("➕ افزودن دامنه", "ssdma")]]
        if mons:
            kb.append([btn("🔄 بررسی دوباره", "sslm"),
                       btn("🗑 حذف دامنه", "sslmd")])
        kb.append([btn("🔙 بازگشت", "dommon"),
                   btn("🏠 خانه", "menu")])
        self._show(chat, mid, "\n".join(lines), kb)

    def _srvmon_home(self, chat: int, mid: int = 0) -> None:
        from monitors import srvmon_cfg
        cfg = srvmon_cfg(self.store)
        txt = "\n".join([
            "📊 مانیتور سرورها", "",
            f'وضعیت: {"▶️ فعال" if cfg.get("enabled") else "⏸ متوقف"}', "",
            "هشدار خودکار وقتی:",
            f'• CPU بیش از {cfg.get("cpuPct")}% برود',
            f'• RAM بیش از {cfg.get("memPct")}% برود',
            f'• دیسک بیش از {cfg.get("diskPct")}% پر شود',
            f'⏱ حداقل فاصلهٔ تکرار هشدار: {cfg.get("cooldownMin")} دقیقه',
            "",
            "برای تغییر هر آستانه، روی آن بزنید."])
        kb = [[btn("⏸ توقف هشدار" if cfg.get("enabled")
                   else "▶️ فعال‌سازی", "srvmontg")],
              [btn(f'⚙️ CPU: {cfg.get("cpuPct")}%', "srvmonh:cpu"),
               btn(f'🧠 RAM: {cfg.get("memPct")}%', "srvmonh:mem")],
              [btn(f'🗄 دیسک: {cfg.get("diskPct")}%', "srvmonh:disk"),
               btn(f'⏱ تکرار: {cfg.get("cooldownMin")}د', "srvmonh:cool")],
              [btn("📊 بررسی الان", "srvmonrun")],
              [btn("🏠 خانه", "menu")]]
        self._show(chat, mid, txt, kb)

    # ---------- Load balancer ----------

    LB_PAGE = 20
    LB_GROUPS_PAGE = 30

    def _lb_zones(self, chat: int, mid: int = 0, flt: str = "all",
                  pg: int = 0) -> None:
        accs = self._cf_accs()
        allz = self._cf_all_zones()
        if not allz:
            self._show(chat, mid, "⚖️ لود بالانسر IP\n\n📭 دامنه‌ای پیدا نشد.",
                       [[btn("🏠 خانه", "menu")]])
            return
        zones = allz if flt == "all" else \
            [z for z in allz if z["_acc"] == int(flt)]
        pages = max(1, (len(zones) + self.LB_PAGE - 1) // self.LB_PAGE)
        pg = max(0, min(pg, pages - 1))
        rows = [btn(f'⚖️ {z["name"]}', f'lbz:{z["_acc"]}:{z["id"]}')
                for z in zones[pg * self.LB_PAGE:(pg + 1) * self.LB_PAGE]]
        kb = grid2(rows)
        kb.append([btn("📁 ✅ همه اکانت‌ها" if flt == "all"
                       else "📁 همه اکانت‌ها", "lbzf:all:0")])
        if len(accs) > 1:
            kb.extend(grid3([
                btn(f'{"👤 ✅ " if flt == str(j) else "👤 "}{a["name"]}',
                    f"lbzf:{j}:0") for j, a in enumerate(accs)]))
        if pages > 1:
            nav = []
            nav.append(btn("⬅️", f"lbzf:{flt}:{pg - 1}") if pg > 0
                       else EMPTY_BTN)
            nav.append(btn("🏠 خانه", "menu"))
            nav.append(btn("➡️", f"lbzf:{flt}:{pg + 1}")
                       if pg < pages - 1 else EMPTY_BTN)
            kb.append(nav)
        else:
            kb.append([btn("🏠 خانه", "menu")])
        label = "همه" if flt == "all" else \
            (accs[int(flt)]["name"] if flt.isdigit()
             and int(flt) < len(accs) else "؟")
        title = f"⚖️ لود بالانسر IP — {label}"
        if pages > 1:
            title += f" — صفحه {pg + 1} از {pages}"
        title += ("\n\nروی دامنه کلیک کن تا ساب‌هاش رو ببینی.\n\n"
                  "• «بالانسر۱/۲/...» وزن هر آی‌پی است (سهمش از ترافیک)\n"
                  "• «🛟 زاپاس» آی‌پی/CNAME رزرو است؛ اگر اصلی فیلتر شد با یک دکمه جایگزین می‌شود")
        self._show(chat, mid, title, kb)

    def _lb_zone_groups(self, chat: int, mid: int, acc: int, zid: str,
                        pg: int = 0) -> None:
        from lb import get_cfg
        accs = self._cf_accs()
        if acc < 0 or acc >= len(accs):
            self._show(chat, mid, "❌ دامنه پیدا نشد.",
                       [[btn("🔙 بازگشت", "lb"), btn("🏠 خانه", "menu")]])
            return
        try:
            client = self._cf_client(acc)
            recs = client.records(zid) or []
            zone = next((z for z in client.zones() or []
                         if z.get("id") == zid), None)
        except CfError as e:
            self._show(chat, mid, f"❌ خطای کلادفلر: {esc(e)}",
                       [[btn("🔙 بازگشت", "lb")]])
            return
        if not zone:
            self._show(chat, mid, "❌ دامنه پیدا نشد.",
                       [[btn("🔙 بازگشت", "lb"), btn("🏠 خانه", "menu")]])
            return
        zname = str(zone["name"])
        groups: dict = {}
        for r in recs:
            if str(r.get("type") or "").upper() not in ("A", "AAAA", "CNAME"):
                continue
            groups.setdefault(str(r.get("name") or ""), []).append(r)
        names = sorted(groups)
        total = sum(len(groups[n]) for n in names)
        pages = max(1, (len(names) + self.LB_GROUPS_PAGE - 1)
                    // self.LB_GROUPS_PAGE)
        pg = max(0, min(pg, pages - 1))
        slice_ = names[pg * self.LB_GROUPS_PAGE:(pg + 1) * self.LB_GROUPS_PAGE]
        lines = [f"⚖️ لود بالانسر IP — {esc(zname)}",
                 f"📦 {len(names)} ساب‌دامین، {total} رکورد فعال", ""]
        kb = []
        if not names:
            lines.append("📭 هنوز لود بالانسری تعریف نشده.")
        else:
            for name in slice_:
                g = groups[name]
                short = "@" if name == zname else \
                    (name[:-(len(zname) + 1)]
                     if name.endswith("." + zname) else name)
                disabled = 0
                try:
                    cfg = get_cfg(self.store, f"lb:cf:{zid}:{name}")
                    disabled = len(cfg.get("disabled") or [])
                except Exception:
                    pass
                lines.append(f"{short} — {len(g)} رکورد"
                             + (f" (+{disabled} غیرفعال)" if disabled else ""))
            for i in range(0, len(slice_), 3):
                row = []
                for j in range(i, min(i + 3, len(slice_))):
                    name = slice_[j]
                    short = "@" if name == zname else \
                        (name[:-(len(zname) + 1)]
                         if name.endswith("." + zname) else name)
                    label = short[:15] + "…" if len(short) > 16 else short
                    idx = pg * self.LB_GROUPS_PAGE + j
                    row.append(btn(label, f"lbgopen:{acc}:{zid}:{idx}",
                                   "plain"))
                kb.extend([row])
            if pages > 1:
                lines += ["", f"صفحه {pg + 1} از {pages}"]
        if pages > 1:
            nav = []
            nav.append(btn("⬅️", f"lbzp:{acc}:{zid}:{pg - 1}") if pg > 0
                       else EMPTY_BTN)
            nav.append(btn("➡️", f"lbzp:{acc}:{zid}:{pg + 1}")
                       if pg < pages - 1 else EMPTY_BTN)
            kb.append(nav)
        kb.append([btn("🔙 بازگشت", "lb"), btn("🏠 خانه", "menu")])
        self._show(chat, mid, "\n".join(lines)[:3500], kb)

    def _lb_open_group(self, chat: int, mid: int, acc: int, zid: str,
                       idx: str) -> None:
        accs = self._cf_accs()
        if acc < 0 or acc >= len(accs):
            self._show(chat, mid, "❌ دامنه پیدا نشد.",
                       [[btn("🔙 بازگشت", "lb")]])
            return
        try:
            client = self._cf_client(acc)
            recs = client.records(zid) or []
        except CfError as e:
            self._show(chat, mid, f"❌ خطای کلادفلر: {esc(e)}",
                       [[btn("🔙 بازگشت", "lb")]])
            return
        names = sorted({str(r.get("name") or "") for r in recs
                        if str(r.get("type") or "").upper()
                        in ("A", "AAAA", "CNAME")})
        try:
            name = names[int(idx)]
        except (ValueError, IndexError):
            self._show(chat, mid, "❌ پیدا نشد.", [[btn("🔙 بازگشت", "lb")]])
            return
        gid = f"lb:cf:{zid}:{name}"
        self.store.put_json(f"lbg:{gid}",
                            {"acc": acc, "zid": zid, "name": name}, 86400)
        self._lb_render(chat, mid, gid)

    def _lb_group(self, acc: int, zid: str, name: str):
        from lb import get_cfg, key_for
        try:
            client = self._cf_client(acc)
            recs = client.records(zid) or []
        except (IndexError, CfError) as e:
            return None, f"❌ خطای کلادفلر: {esc(e)}"
        group = [r for r in recs
                 if str(r.get("name") or "") == name
                 and str(r.get("type") or "").upper()
                 in ("A", "AAAA", "CNAME")]
        key = key_for(zid, name)
        return {"client": client, "group": group,
                "cfg": get_cfg(self.store, key), "key": key}, ""

    def _lb_settings(self, chat: int, mid: int, rid: str) -> None:
        from lb import new_gid
        acc, zid = self._cf_ctx(chat, rid)
        if not zid:
            self._show(chat, mid, "⏳ نشست منقضی شد.", home_kb())
            return
        ctx, err = self._lb_group(acc, zid, "")
        if err and not ctx:
            self._show(chat, mid, err, home_kb())
            return
        # find record name
        try:
            recs = ctx["client"].records(zid) or []
        except CfError as e:
            self._show(chat, mid, f"❌ {esc(e)}", home_kb())
            return
        r = next((x for x in recs if x.get("id") == rid), None)
        if not r:
            self._show(chat, mid, "❌ رکورد پیدا نشد.", home_kb())
            return
        gid = new_gid(self.store, acc, zid, str(r.get("name")))
        self.store.put_json(f"lbg:{gid}",
                            {"acc": acc, "zid": zid,
                             "name": str(r.get("name")), "rid": rid}, 86400)
        self._lb_render(chat, mid, gid)

    def _lb_settings_gid(self, chat: int, mid: int, gid: str) -> None:
        self._lb_render(chat, mid, gid)

    def _lb_render(self, chat: int, mid: int, gid: str) -> None:
        from lb import get_cfg, entries_of, fa_num
        sess = self.store.get_json(f"lbg:{gid}", {}) or {}
        acc, zid, name = sess.get("acc", 0), sess.get("zid", ""), \
            sess.get("name", "")
        if not zid or not name:
            self._show(chat, mid, "⏳ نشست منقضی شد.", home_kb())
            return
        try:
            client = self._cf_client(acc)
            recs = client.records(zid) or []
        except (IndexError, CfError) as e:
            self._show(chat, mid, f"❌ {esc(e)}", home_kb())
            return
        group = [r for r in recs
                 if str(r.get("name") or "") == name
                 and str(r.get("type") or "").upper()
                 in ("A", "AAAA", "CNAME")]
        key = f"lb:cf:{zid}:{name}"
        cfg = get_cfg(self.store, key)
        entries = entries_of(group, cfg)
        on_n = sum(1 for e in entries if e["on"])
        lines = ["⚖️ تنظیمات لود بالانسر IP", "",
                 f"📛 ساب‌دامین: <code>{esc(name)}</code>",
                 f"🌍 آی‌پی‌ها: {on_n} فعال از {len(entries)}", ""]
        kb = []
        if not entries:
            lines.append("📭 هنوز آی‌پی‌ای اضافه نشده.")
        else:
            for i, e in enumerate(entries):
                lines.append(f'{i + 1}) {"🟢" if e["on"] else "🔴"} '
                             f'<code>{esc(e["content"])}</code> — وزن {e["weight"]}'
                             + ("" if e["on"] else " (غیرفعال)"))
            lines += ["",
                      "وزن و فعال/غیرفعال هر آی‌پی را از دکمه‌های زیر تغییر بده؛ زاپاس را از دکمهٔ 🛟 زیر."]
            for i, e in enumerate(entries):
                kb.append([btn(f"بالانسر{str(i + 1).translate(str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹'))}",
                               f"lbw:{gid}:{i}"),
                           btn("⏸ غیرفعال" if e["on"] else "▶️ فعال",
                               f"lbt:{gid}:{i}")])
        kb.append([btn("➕ افزودن آی‌پی", f"lbadd:{gid}")])
        if on_n > 0:
            kb.append([btn("🟠 Proxy همه روشن", f"lbp:{gid}:on"),
                       btn("⚪️ Proxy همه خاموش", f"lbp:{gid}:off")])
        kb.append([btn("🛟 آیپی زاپاس", f"lbsh:{gid}", "success")])
        rid = sess.get("rid", "")
        kb.append([btn("🔙 بازگشت", f"cfr:{rid}" if rid else "zones")])
        self._show(chat, mid, "\n".join(lines), kb)

    def _lb_weight_ask(self, chat: int, mid: int, gid: str, idx: int) -> None:
        self.store.put_json(f"pend:{chat}",
                            {"type": "lb_weight", "gid": gid,
                             "idx": idx}, 600)
        self._show(chat, mid,
                   "⚖️ وزن جدید را بفرستید (عدد ۰ یا بیشتر):",
                   [[btn("⬅️ انصراف", f"lbsr:{gid}")]])

    def _lb_toggle_ask(self, chat: int, mid: int, gid: str, idx: int) -> None:
        from lb import entries_of, get_cfg, key_for
        sess = self.store.get_json(f"lbg:{gid}", {}) or {}
        entries = self._lb_entries(sess)
        if idx >= len(entries):
            self._show(chat, mid, "❌ مورد پیدا نشد.", home_kb())
            return
        e = entries[idx]
        self._show(chat, mid,
                   (f'⏸ این آی‌پی از لود بالانسر حذف (غیرفعال) شود؟'
                    if e["on"] else
                    f'▶️ این آی‌پی به لود بالانسر اضافه (فعال) شود؟')
                   + f"\n\n<code>{esc(e['content'])}</code>",
                   [[btn("✅ بله", f"lbtc:{gid}:{idx}"),
                     btn("❌ انصراف", f"lbsr:{gid}")]])

    def _lb_entries(self, sess: dict):
        from lb import entries_of, get_cfg
        try:
            client = self._cf_client(sess.get("acc", 0))
            recs = client.records(sess.get("zid", "")) or []
        except (IndexError, CfError):
            return []
        group = [r for r in recs
                 if str(r.get("name") or "") == sess.get("name")
                 and str(r.get("type") or "").upper()
                 in ("A", "AAAA", "CNAME")]
        return entries_of(group, get_cfg(
            self.store, f'lb:cf:{sess.get("zid")}:{sess.get("name")}'))

    def _lb_toggle_do(self, chat: int, mid: int, gid: str, idx: int) -> None:
        from lb import get_cfg, save_cfg, key_for
        sess = self.store.get_json(f"lbg:{gid}", {}) or {}
        entries = self._lb_entries(sess)
        if idx >= len(entries):
            self._show(chat, mid, "❌ مورد پیدا نشد.", home_kb())
            return
        e = entries[idx]
        key = key_for(sess.get("zid", ""), sess.get("name", ""))
        cfg = get_cfg(self.store, key)
        try:
            client = self._cf_client(sess.get("acc", 0))
            if e["on"]:
                if e["id"]:
                    client.del_record(sess["zid"], e["id"])
                dis = cfg.get("disabled") or []
                dis.append({"type": e["type"], "content": e["content"],
                            "proxied": e["proxied"], "ttl": e["ttl"]})
                cfg["disabled"] = dis
            else:
                client.add_record(sess["zid"], e["type"],
                                  sess["name"], e["content"],
                                  e["ttl"] or 1, bool(e["proxied"]))
                cfg["disabled"] = [d for d in (cfg.get("disabled") or [])
                                   if str((d or {}).get("content")) != e["content"]]
            save_cfg(self.store, key, cfg)
        except (IndexError, CfError) as ex:
            self._show(chat, mid, f"❌ خطا: {esc(ex)}", home_kb())
            return
        self._lb_render(chat, mid, gid)

    def _lb_add_start(self, chat: int, mid: int, gid: str) -> None:
        self.store.put_json(f"pend:{chat}",
                            {"type": "lb_add", "gid": gid}, 600)
        self._show(chat, mid,
                   "➕ نوع و مقدار آی‌پی جدید را بفرستید (مثلاً <code>A 1.2.3.4</code>):",
                   [[btn("⬅️ انصراف", f"lbsr:{gid}")]])

    def _lb_proxy_all(self, chat: int, mid: int, gid: str, on: bool) -> None:
        from lb import get_cfg
        sess = self.store.get_json(f"lbg:{gid}", {}) or {}
        entries = self._lb_entries(sess)
        try:
            client = self._cf_client(sess.get("acc", 0))
            for e in entries:
                if not e["on"] or not e["id"]:
                    continue
                try:
                    client.update_record(sess["zid"],
                                         {"id": e["id"], "type": e["type"],
                                          "name": sess["name"],
                                          "content": e["content"],
                                          "ttl": e["ttl"], "proxied": True},
                                         proxied=on)
                except CfError:
                    pass
        except (IndexError, CfError) as ex:
            self._show(chat, mid, f"❌ {esc(ex)}", home_kb())
            return
        self._lb_render(chat, mid, gid)

    def _lb_spare_home(self, chat: int, mid: int, rid: str) -> None:
        from lb import new_gid
        acc, zid = self._cf_ctx(chat, rid)
        if not zid:
            self._show(chat, mid, "⏳ نشست منقضی شد.", home_kb())
            return
        try:
            recs = self._cf_client(acc).records(zid) or []
        except (IndexError, CfError) as e:
            self._show(chat, mid, f"❌ {esc(e)}", home_kb())
            return
        r = next((x for x in recs if x.get("id") == rid), None)
        if not r:
            self._show(chat, mid, "❌ رکورد پیدا نشد.", home_kb())
            return
        gid = new_gid(self.store, acc, zid, str(r.get("name")))
        self.store.put_json(f"lbg:{gid}",
                            {"acc": acc, "zid": zid,
                             "name": str(r.get("name")), "rid": rid}, 86400)
        self._lb_spare_home_gid(chat, mid, gid)

    def _lb_spare_home_gid(self, chat: int, mid: int, gid: str) -> None:
        from lb import get_cfg, spare_of
        sess = self.store.get_json(f"lbg:{gid}", {}) or {}
        entries = self._lb_entries(sess)
        cfg = get_cfg(self.store,
                      f'lb:cf:{sess.get("zid")}:{sess.get("name")}')
        lines = ["🛟 زاپاس گروه", ""]
        kb = []
        for i, e in enumerate(entries):
            if not e["on"]:
                continue
            sp = spare_of(cfg, e["content"])
            lines.append(f'{i + 1}) <code>{esc(e["content"])}</code> → '
                         + (f'<code>{esc(sp["content"])}</code>' if sp
                            else "بدون زاپاس"))
            kb.append([btn(f'🛟 {e["content"][:20]}', f"lbsm:{gid}:{i}")])
        kb.append([btn("🔙 بازگشت", f'lbsr:{gid}' if False else f'lbs:{sess.get("rid", "")}' if sess.get("rid") else "zones"),
                   btn("🏠 خانه", "menu")])
        self._show(chat, mid, "\n".join(lines), kb)

    def _lb_spare_menu(self, chat: int, mid: int, gid: str, idx: int) -> None:
        from lb import get_cfg, spare_of
        sess = self.store.get_json(f"lbg:{gid}", {}) or {}
        entries = [e for e in self._lb_entries(sess) if e["on"]]
        on_entries = entries
        if idx >= len(on_entries):
            self._show(chat, mid, "❌ مورد پیدا نشد.", home_kb())
            return
        e = on_entries[idx]
        # map back to full-list index
        full = self._lb_entries(sess)
        fi = next((k for k, x in enumerate(full)
                   if x["content"] == e["content"] and x["on"]), idx)
        cfg = get_cfg(self.store,
                      f'lb:cf:{sess.get("zid")}:{sess.get("name")}')
        sp = spare_of(cfg, e["content"])
        lines = [f'🛟 زاپاس — <code>{esc(e["content"])}</code> ({e["type"]})', "",
                 f'زاپاس فعلی: <code>{esc(sp["content"])}</code> ({sp["type"]})' if sp
                 else "هنوز زاپاسی تعیین نشده.", "",
                 "اگر این آی‌پی فیلتر شد، با «🔄 جایگزینی» رکورد DNS به زاپاس عوض می‌شود؛",
                 "بعداً با همان دکمه می‌توانی به اصلی برگردی."]
        kb = [[btn("✏️ تغییر زاپاس" if sp else "➕ تعیین زاپاس",
                   f"lbsset:{gid}:{fi}")]]
        if sp:
            kb.append([btn(f'🔄 جایگزینی با {sp["content"][:24]}',
                           f"lbsgo:{gid}:{fi}")])
            kb.append([btn("❌ حذف زاپاس", f"lbsdel:{gid}:{fi}")])
        kb.append([btn("🔙 بازگشت", f"lbsh:{gid}"),
                   btn("🏠 خانه", "menu")])
        self._show(chat, mid, "\n".join(lines), kb)

    def _lb_replace(self, chat: int, mid: int, gid: str, idx: int,
                    confirm: bool) -> None:
        from lb import get_cfg, spare_of, save_cfg
        sess = self.store.get_json(f"lbg:{gid}", {}) or {}
        entries = self._lb_entries(sess)
        if idx >= len(entries):
            self._show(chat, mid, "❌ مورد پیدا نشد.", home_kb())
            return
        e = entries[idx]
        cfg = get_cfg(self.store,
                      f'lb:cf:{sess.get("zid")}:{sess.get("name")}')
        sp = spare_of(cfg, e["content"])
        if not sp:
            self._show(chat, mid, "❌ زاپاسی تعیین نشده.", home_kb())
            return
        if not confirm:
            self._show(chat, mid,
                       f'🔄 رکورد <code>{esc(e["content"])}</code> به '
                       f'<code>{esc(sp["content"])}</code> عوض شود؟',
                       [[btn("✅ بله", f"lbsgoc:{gid}:{idx}"),
                         btn("❌ انصراف", f"lbsm:{gid}:{idx}")]])
            return
        try:
            client = self._cf_client(sess.get("acc", 0))
            if e["id"]:
                client.del_record(sess["zid"], e["id"])
            client.add_record(sess["zid"], sp.get("type", e["type"]),
                              sess["name"], sp["content"], 1, False)
        except (IndexError, CfError) as ex:
            self._show(chat, mid, f"❌ نشد: {esc(ex)}", home_kb())
            return
        self.bot.send(chat, "✅ جایگزینی انجام شد.")
        self._lb_render(chat, 0, gid)

    def _lb_spare_del(self, chat: int, mid: int, gid: str, idx: int) -> None:
        from lb import get_cfg, save_cfg
        sess = self.store.get_json(f"lbg:{gid}", {}) or {}
        entries = [e for e in self._lb_entries(sess) if e["on"]]
        if idx >= len(entries):
            self._show(chat, mid, "❌ مورد پیدا نشد.", home_kb())
            return
        key = f'lb:cf:{sess.get("zid")}:{sess.get("name")}'
        cfg = get_cfg(self.store, key)
        (cfg.get("spares") or {}).pop(entries[idx]["content"], None)
        save_cfg(self.store, key, cfg)
        self.bot.send(chat, "✅ زاپاس حذف شد.")
        self._lb_spare_home_gid(chat, 0, gid)

    def _cf_quota(self, chat: int, mid: int) -> None:
        from cfusage import account_usage
        accs = self._cf_accs()
        if not accs:
            self._show(chat, mid, "❌ اکانت کلادفلر تنظیم نشده.", home_kb())
            return
        lines = ["☁️ سهمیهٔ کلادفلر (امروز)", ""]
        kb = []
        for a in accs:
            try:
                u = account_usage(a["token"])
            except Exception as e:
                lines.append(f'• {a["name"]}: خطا ({str(e)[:60]})')
                continue
            kv = u.get("kv") or {}
            lines.append(f'• {a["name"]}:')
            lines.append(f'  🚦 درخواست‌ها: {u.get("requests", "?")} / ۱۰۰٬۰۰۰')
            lines.append(f'  🗄 KV خواندن: {kv.get("read", "?")} / ۱۰۰٬۰۰۰')
            lines.append(f'  ✍️ KV نوشتن: {kv.get("write", "?")} / ۱٬۰۰۰')
            lines.append(f'  🗑 KV حذف: {kv.get("delete", "?")} / ۱٬۰۰۰')
            st = u.get("storage") or {}
            if st:
                lines.append(f'  💾 حجم KV: {st.get("bytes", "?")} بایت · {st.get("keys", "?")} کلید')
        kb.append([btn("🔙 بازگشت", "dommon"),
                   btn("🏠 خانه", "menu")])
        self._show(chat, mid, "\n".join(lines), kb)

    def _srv_import(self, chat: int, mid: int) -> None:
        self._show(chat, mid, "⏳ در حال خواندن سرورها از دیتاسنترها…",
                   home_kb())
        found = []
        for a in self._hz_accs():
            try:
                for s in Hz(a["token"]).all("/servers"):
                    ip = ((s.get("public_net") or {}).get("ipv4") or {}).get("ip")
                    if ip:
                        found.append({"host": ip, "name": s.get("name") or ip,
                                      "note": f'Hetzner: {a.get("name")}'})
            except HzError:
                continue
        for a in self._ln_accs():
            try:
                from ln import Ln
                for s in Ln(a["token"]).all("/linode/instances"):
                    ip = (s.get("ipv4") or [None])[0]
                    if ip:
                        found.append({"host": ip, "name": s.get("label") or ip,
                                      "note": f'Linode: {a.get("name")}'})
            except Exception:
                continue
        lst = self._srv_list()
        have = {str(s.get("host") or "").lower() for s in lst}
        import time as _t
        import random as _r
        n = 0
        for f in found:
            if str(f["host"]).lower() in have:
                continue
            lst.append({"id": f's{int(_t.time())}{_r.randint(10, 99)}{n}',
                        "name": f["name"], "host": f["host"],
                        "user": "root", "port": 22, "auth": "pass",
                        "secret": "", "note": f.get("note", "")})
            n += 1
        self.store.put_json("srv_list", lst)
        self.bot.send(chat,
                      f"✅ {n} سرور تازه اضافه شد." + (" (رمز ندارند؛ ویرایش کنید.)" if n else ""),
                      [[btn("🖥 سرورها و مانیتورینگ", "srv")],
                       [btn("🏠 خانه", "menu")]])

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
            btn("🔄 ریست", f"lnsu:{i}:{sid}:reset"),
            btn("🔓 ریست رمز", f"lnsu:{i}:{sid}:pass"),
            btn("✏️ تغییر نام", f"lnsu:{i}:{sid}:rename"),
            btn("🛠️ ریبیلد", f"lnsu:{i}:{sid}:rebuild"),
            btn("⬆️ ارتقا", f"lnsu:{i}:{sid}:upgrade"),
            btn("📷 اسنپ‌شات", f"lnsu:{i}:{sid}:snap"),
            btn("🗑 حذف اسنپ‌شات", f"lnsu:{i}:{sid}:dsnap"),
            btn("➕ IPv4", f"lnsu:{i}:{sid}:a4"),
            btn("➕ IPv6", f"lnsu:{i}:{sid}:a6"),
            btn("❌ IPv4", f"lnsu:{i}:{sid}:u4"),
            btn("❌ IPv6", f"lnsu:{i}:{sid}:u6"),
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
        multi = len(accs) > 1
        nm = accs[sel]["name"]
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
        kb.append([btn(multi and f"➕ ساخت سرور در {nm}" or "➕ ساخت سرور",
                       f"hzsc:{sel}")])
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

    def _hz_pick_plan(self, chat: int, mid: int, i: int,
                      loc: str) -> None:
        try:
            tok = self._hz_accs()[i]["token"]
        except IndexError:
            self._show(chat, mid, "❌ اکانت پیدا نشد.", home_kb())
            return
        pend = self.store.get_json(f"pend:{chat}", {}) or {}
        if pend.get("type") != "hz_srv_remark":
            self._show(chat, mid, "⏳ عملیات منقضی شده.", home_kb())
            return
        try:
            types = [t for t in Hz(tok).all("/server_types")
                     if not t.get("deprecated")]
        except HzError as e:
            self._show(chat, mid, f"❌ {esc(e)}", home_kb())
            return
        avail = [t for t in types
                 if any(x.get("location") == loc
                        for x in (t.get("prices") or []))]
        avail.sort(key=lambda t: float(
            next((x for x in (t.get("prices") or [])
                  if x.get("location") == loc), {}).get("price_monthly", {})
            .get("net", 1e9) or 1e9))
        pend["location"] = loc
        self.store.put_json(f"pend:{chat}", pend, 600)
        fam_label = {"cpx": "🧩 CPX · پردازنده AMD",
                     "cx": "🧩 CX · پردازنده Intel",
                     "cax": "🧩 CAX · پردازنده Ampere ARM",
                     "ccx": "🧩 CCX · اختصاصی (Dedicated)",
                     "other": "🧩 سایر پلن‌ها"}
        kb = []
        groups: dict = {}
        for t in avail[:60]:
            nm = str(t.get("name", "")).lower()
            fam = next((f for f in ("cpx", "cx", "cax", "ccx")
                        if nm.startswith(f)), "other")
            groups.setdefault(fam, []).append(t)
        for fam in ("cpx", "cx", "cax", "ccx", "other"):
            arr = groups.get(fam) or []
            if not arr:
                continue
            kb.append([btn(fam_label[fam], "noop", "plain")])
            rows = []
            for t in arr:
                pm = next((x for x in (t.get("prices") or [])
                           if x.get("location") == loc), {})
                eur = ((pm.get("price_monthly") or {}).get("gross")
                       or "?")
                rows.append(btn(f'{t.get("name")} · {eur}€',
                                f'hzscp:{i}:{t.get("id")}'))
            kb.extend(grid2(rows))
        kb.append([btn("🔙 بازگشت", "hz"),
                   btn("🏠 خانه", "menu")])
        self._show(chat, mid,
                   f"💰 پلن سرور را انتخاب کن (لوکیشن: {loc}):", kb)

    def _hz_pick_image(self, chat: int, mid: int, i: int,
                       plan: str) -> None:
        try:
            tok = self._hz_accs()[i]["token"]
        except IndexError:
            self._show(chat, mid, "❌ اکانت پیدا نشد.", home_kb())
            return
        pend = self.store.get_json(f"pend:{chat}", {}) or {}
        if pend.get("type") != "hz_srv_remark":
            self._show(chat, mid, "⏳ عملیات منقضی شده.", home_kb())
            return
        try:
            stype = Hz(tok).call(f"/server_types/{plan}").get("server_type") \
                or {}
            imgs = Hz(tok).all("/images")
        except HzError as e:
            self._show(chat, mid, f"❌ {esc(e)}", home_kb())
            return
        arch = stype.get("architecture")
        avail = [im for im in imgs
                 if im.get("type") in ("system", "snapshot")
                 and (not arch or im.get("architecture") == arch)]
        avail.sort(key=lambda x: 0 if x.get("type") == "system" else 1)
        if not avail:
            self._show(chat, mid, "❌ تصویری پیدا نشد.", home_kb())
            return
        pend["planId"] = plan
        self.store.put_json(f"pend:{chat}", pend, 600)
        kb = grid2([btn(str(im.get("name") or im.get("description")
                           or im.get("id")),
                         f'hzsci:{i}:{im.get("id")}') for im in avail[:50]])
        kb.append([btn("🔙 بازگشت", "hz"),
                   btn("🏠 خانه", "menu")])
        self._show(chat, mid, "🖼️ تصویر (سیستم‌عامل) را انتخاب کنید:", kb)

    def _hz_create(self, chat: int, mid: int, i: int, img: str) -> None:
        import random as _r
        import string as _s
        try:
            tok = self._hz_accs()[i]["token"]
        except IndexError:
            self._show(chat, mid, "❌ اکانت پیدا نشد.", home_kb())
            return
        pend = self.store.get_json(f"pend:{chat}", {}) or {}
        if pend.get("type") != "hz_srv_remark" or "location" not in pend \
                or "planId" not in pend:
            self._show(chat, mid, "⏳ عملیات منقضی شده.", home_kb())
            return
        pw = "".join(_r.choice(_s.ascii_letters + _s.digits)
                     for _ in range(16))
        cloud_init = ("#cloud-config\n"
                      "ssh_pwauth: true\n"
                      "chpasswd:\n"
                      f"  list: |\n    root:{pw}\n"
                      "  expire: False\n")
        try:
            r = Hz(tok).call("/servers", "POST",
                             {"name": pend["remark"],
                              "server_type": str(pend["planId"]),
                              "image": str(img),
                              "location": str(pend["location"]),
                              "start_after_create": True,
                              "user_data": cloud_init})
        except HzError as e:
            self._show(chat, mid, f"❌ ساخت ناموفق بود:\n{esc(e)}",
                       home_kb())
            return
        if (r or {}).get("error"):
            from hz import err_fa
            self._show(chat, mid,
                       f"❌ ساخت ناموفق بود:\n{esc(err_fa(r))}", home_kb())
            return
        self.store.delete(f"pend:{chat}")
        hs = (r or {}).get("server") or {}
        ip = ((hs.get("public_net") or {}).get("ipv4") or {}).get("ip", "")
        if ip:
            lst = self._srv_list()
            import time as _t
            lst.append({"id": f's{int(_t.time())}{_r.randint(10, 99)}',
                        "name": hs.get("name") or pend["remark"],
                        "host": ip, "user": "root", "port": 22,
                        "auth": "pass", "secret": pw,
                        "note": f'Hetzner: {hs.get("name") or ""}'})
            self.store.put_json("srv_list", lst)
        self.bot.send(chat,
                      "🎉 سرور هتزنر ساخته شد.\n"
                      f"🔑 رمز root:\n<code>{esc(pw)}</code>\n\n"
                      "⚠️ آن را امن نگه دار؛ دوباره نمایش داده نمی‌شود."
                      + (f"\n🌐 آی‌پی: <code>{esc(ip)}</code>" if ip else ""),
                      [[btn("🖥 سرورها", f"hzs:{i}:0")]])

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