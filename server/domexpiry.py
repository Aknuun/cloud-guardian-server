"""Domain expiry monitor: whois(.ir) + RDAP + optional WhoisXMLAPI."""
from __future__ import annotations

import datetime
import json
import re
import socket
import urllib.parse
import urllib.request

TEHRAN_OFFSET_MS = 12600000  # +03:30
DOMEXP_ALERT_DAYS = [60, 30, 14, 7, 3, 1, 0]
DEFAULT_PROVIDER = "whoisxmlapi_domaininfo"

_UA = "Mozilla/5.0 (compatible; CloudGuardianBot/1.0)"


def _now_ms() -> int:
    return int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)


def whois_server_for(domain: str):
    return "whois.nic.ir" if str(domain or "").lower().endswith(".ir") else None


def whois_query(server: str, domain: str, timeout: int = 12):
    try:
        with socket.create_connection((server, 43), timeout=timeout) as s:
            s.sendall((domain + "\r\n").encode())
            s.settimeout(timeout)
            buf = b""
            while len(buf) < 60000:
                try:
                    chunk = s.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                buf += chunk
        return buf.decode("utf-8", "replace")
    except Exception:
        return None


def parse_ir_whois(text: str) -> dict:
    if not text:
        return {"status": "error"}
    if re.search(r"%ERROR:101", text) or re.search(r"no entries found", text, re.I):
        return {"status": "not_found"}
    m = re.search(r"expir[ye][d]?[\s_-]*date\s*[:=]\s*(\d{4})-(\d{2})-(\d{2})",
                  text, re.I)
    if m:
        dt = datetime.datetime(int(m[1]), int(m[2]), int(m[3]), 12, 0,
                               tzinfo=datetime.timezone.utc)
        return {"status": "ok", "expiry": int(dt.timestamp() * 1000)}
    if re.search(r"hidden by the domain holder", text, re.I):
        return {"status": "hidden"}
    if re.search(r"available for registration", text, re.I):
        return {"status": "available"}
    if re.search(r"domain:\s*\S+", text, re.I):
        return {"status": "no_date"}
    return {"status": "error"}


def rdap_query(domain: str, timeout: int = 15):
    url = "https://rdap.org/domain/" + urllib.parse.quote(domain)
    req = urllib.request.Request(url, headers={
        "accept": "application/rdap+json", "user-agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def parse_rdap_expiry(data) -> int | None:
    if not data or not isinstance(data.get("events"), list):
        return None
    for e in data["events"]:
        if e and e.get("eventAction") == "expiration" and e.get("eventDate"):
            try:
                s = e["eventDate"].replace("Z", "+00:00")
                return int(datetime.datetime.fromisoformat(s)
                           .timestamp() * 1000)
            except Exception:
                return None
    return None


def _find_expiry_in_json(obj):
    hits = []
    seen = set()

    def walk(o, path):
        if not o or not isinstance(o, (dict, list)) or id(o) in seen:
            return
        seen.add(id(o))
        items = o.items() if isinstance(o, dict) else enumerate(o)
        for k, v in items:
            kl = str(k).lower()
            p = f"{path}.{kl}" if path else kl
            if isinstance(v, str) and "expir" in kl:
                ms = _parse_any_date(v)
                if ms:
                    hits.append({"ms": ms, "path": p})
            elif isinstance(v, (dict, list)):
                walk(v, p)

    walk(obj, "")
    if not hits:
        return None
    reg = next((h for h in hits if "registrydata" in h["path"]), None)
    return (reg or hits[0])["ms"]


def _parse_any_date(s: str):
    s = str(s).strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d"):
        try:
            if fmt is None:
                dt = datetime.datetime.fromisoformat(s)
            else:
                dt = datetime.datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return int(dt.timestamp() * 1000)
        except Exception:
            continue
    return None


def whois_xml_domain_info(domain: str, key: str, timeout: int = 20) -> dict:
    url = ("https://domain-info.whoisxmlapi.com/api/v1?apiKey="
           + urllib.parse.quote(key) + "&domainName="
           + urllib.parse.quote(domain))
    try:
        req = urllib.request.Request(url, headers={"accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
        exp = _find_expiry_in_json(data)
        if exp:
            return {"status": "ok", "expiry": exp}
        return {"status": "no_date"}
    except Exception as e:
        return {"status": "api_error", "message": str(e)[:220]}


def domain_expiry_via_api(domain: str, key: str) -> dict | None:
    if not key:
        return None
    return whois_xml_domain_info(domain, key)


def fetch_domain_expiry(domain: str, key: str = "") -> dict:
    d = str(domain or "").lower().strip()
    if not re.match(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$", d):
        return {"status": "invalid"}
    ws = whois_server_for(d)
    if ws:
        r = parse_ir_whois(whois_query(ws, d))
        if r["status"] == "ok":
            return r
        if r["status"] in ("hidden", "no_date", "error"):
            api = domain_expiry_via_api(d, key)
            if api and api.get("status") == "ok":
                return api
            if r["status"] == "error" and api \
                    and api.get("status") == "api_error":
                return api
        return r
    data = rdap_query(d)
    exp = parse_rdap_expiry(data)
    if exp:
        return {"status": "ok", "expiry": exp}
    api = domain_expiry_via_api(d, key)
    if api and api.get("status") == "ok":
        return api
    if data:
        return {"status": "no_date"}
    return {"status": "error"}


def remaining(expiry_ms, now_ms=None) -> str:
    now = now_ms or _now_ms()
    if not isinstance(expiry_ms, (int, float)):
        return "؟"
    if expiry_ms <= now:
        return "منقضی شده"
    a = datetime.datetime.fromtimestamp((now + TEHRAN_OFFSET_MS) / 1000,
                                        datetime.timezone.utc)
    b = datetime.datetime.fromtimestamp((expiry_ms + TEHRAN_OFFSET_MS) / 1000,
                                        datetime.timezone.utc)
    years = b.year - a.year
    months = b.month - a.month
    days = b.day - a.day
    if days < 0:
        months -= 1
        first_of_b = datetime.datetime(b.year, b.month, 1,
                                       tzinfo=datetime.timezone.utc)
        prev_last = first_of_b - datetime.timedelta(days=1)
        days += prev_last.day
    if months < 0:
        years -= 1
        months += 12
    parts = []
    if years > 0:
        parts.append(f"{years} سال")
    if months > 0:
        parts.append(f"{months} ماه")
    if days > 0 or not parts:
        parts.append(f"{days} روز")
    return " و ".join(parts)


def emoji(item: dict, now_ms=None) -> str:
    if item.get("manual"):
        return "✍️"
    st = item.get("status")
    if st == "not_found":
        return "❔"
    if st in ("hidden", "no_date", "available"):
        return "⚪"
    exp = item.get("expiry")
    if st != "ok" or not isinstance(exp, (int, float)):
        return "❓"
    days = -(-(exp - (now_ms or _now_ms())) // 86400000)
    if days < 0:
        return "⛔"
    if days <= 7:
        return "🔴"
    if days <= 30:
        return "🟠"
    if days <= 90:
        return "🟡"
    return "🟢"


def line(item: dict, now_ms=None) -> str:
    em = emoji(item, now_ms)
    st = item.get("status")
    exp = item.get("expiry")
    if (item.get("manual") or st == "ok") and isinstance(exp, (int, float)):
        tail = remaining(exp, now_ms)
    elif st == "hidden":
        tail = "تاریخ مخفی (دستی ثبت کنید)"
    elif st == "no_date":
        tail = "تاریخ اعلام نشده"
    elif st == "not_found":
        tail = "در ایرنیک ثبت نیست"
    elif st == "available":
        tail = "آزاد / ثبت‌نشده"
    elif st == "api_error":
        tail = "خطای API انقضا (کلید/اعتبار)"
    else:
        tail = "خطا در استعلام"
    return f'{em} {item.get("domain")} — {tail}'
