"""Load-balancer groups over same-name DNS records.

Group key: lb:cf:<zone_id>:<name> -> {weights:{content:n}, disabled:[...],
spares:{content:{type,content}}}. gid sessions: lbg:<gid> -> {acc,zid,name}.
"""
from __future__ import annotations

import secrets

LB_TYPES = ("A", "AAAA", "CNAME")
FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def fa_num(n) -> str:
    return str(n).translate(FA_DIGITS)


def is_lb_rec(r: dict) -> bool:
    return str((r or {}).get("type") or "").upper() in LB_TYPES


def key_for(zid: str, name: str) -> str:
    return f"lb:cf:{zid}:{name}"


def get_cfg(store, key: str) -> dict:
    cfg = store.get_json(key, {}) or {}
    if not isinstance(cfg.get("weights"), dict):
        cfg["weights"] = {}
    if not isinstance(cfg.get("disabled"), list):
        cfg["disabled"] = []
    if not isinstance(cfg.get("spares"), dict):
        cfg["spares"] = {}
    return cfg


def save_cfg(store, key: str, cfg: dict) -> None:
    store.put_json(key, cfg)


def entries_of(group: list, cfg: dict) -> list:
    out = []
    for r in group or []:
        out.append({"on": True, "id": r.get("id"),
                    "type": str(r.get("type") or "").upper(),
                    "content": str(r.get("content") or ""),
                    "proxied": bool(r.get("proxied")),
                    "ttl": r.get("ttl"),
                    "weight": (cfg.get("weights") or {}).get(
                        str(r.get("content"))) or 1})
    for d in cfg.get("disabled") or []:
        d = d or {}
        out.append({"on": False, "id": None,
                    "type": str(d.get("type") or "").upper(),
                    "content": str(d.get("content")),
                    "proxied": bool(d.get("proxied")),
                    "ttl": d.get("ttl"),
                    "weight": (cfg.get("weights") or {}).get(
                        str(d.get("content"))) or 1})
    out.sort(key=lambda e: e["content"])
    return out


def spare_of(cfg: dict, content: str):
    return (cfg.get("spares") or {}).get(str(content))


def new_gid(store, acc: int, zid: str, name: str) -> str:
    gid = secrets.token_hex(4)
    store.put_json(f"lbg:{gid}", {"acc": acc, "zid": zid, "name": name},
                   86400)
    return gid


def load_gid(store, gid: str):
    return store.get_json(f"lbg:{gid}", {}) or {}
