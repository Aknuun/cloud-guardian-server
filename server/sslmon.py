"""SSL certificate monitor. Direct TLS handshake, no APIs."""
from __future__ import annotations

import datetime
import socket
import ssl


def check_host(host: str, timeout: int = 20) -> dict:
    """Returns {host, ok, days, expiry, error}."""
    host = str(host or "").strip().lower()
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
    except Exception as e:
        return {"host": host, "ok": False, "error": str(e)[:150]}
    try:
        exp = datetime.datetime.strptime(cert["notAfter"],
                                         "%b %d %H:%M:%S %Y %Z")
        exp = exp.replace(tzinfo=datetime.timezone.utc)
        days = (exp - datetime.datetime.now(datetime.timezone.utc)).days
        return {"host": host, "ok": True, "days": days,
                "expiry": exp.isoformat()[:10]}
    except Exception as e:
        return {"host": host, "ok": False, "error": str(e)[:150]}


def result_line(it: dict, warn_days: int = 5) -> str:
    if not it.get("ok"):
        return (f'🔴 <code>{it["host"]}</code> — خطا: '
                f'{it.get("error", "?")[:80]}')
    d = it.get("days", 999)
    em = "🔴" if d <= warn_days else "🟢"
    return f'{em} <code>{it["host"]}</code> — {d} روز مانده'


def get_monitors(store) -> list:
    lst = store.get_json("ssl_monitor", [])
    return lst if isinstance(lst, list) else []


def check_all(store) -> list:
    return [check_host(m.get("host")) for m in get_monitors(store)]
