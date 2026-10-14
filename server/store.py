"""SQLite-backed key/value store for the on-server bot.

Single file, no external services. Values are TEXT; expiry is enforced
lazily on read plus periodic purge. Thread-safe via one lock.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time


_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL,
  exp INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_kv_exp ON kv(exp);
"""


class Store:
    def __init__(self, path: str):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._db.execute("PRAGMA journal_mode=WAL;")
        self._db.execute("PRAGMA synchronous=NORMAL;")
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def _now(self) -> int:
        return int(time.time())

    def get(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT v, exp FROM kv WHERE k = ?", (key,)
            ).fetchone()
        if not row:
            return None
        val, exp = row
        if exp and exp <= self._now():
            self.delete(key)
            return None
        return val

    def put(self, key: str, value: str, ttl_sec: int = 0) -> None:
        exp = self._now() + int(ttl_sec) if ttl_sec and ttl_sec > 0 else 0
        with self._lock:
            self._db.execute(
                "INSERT INTO kv(k, v, exp) VALUES(?, ?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v, exp=excluded.exp",
                (key, value, exp),
            )
            self._db.commit()

    def delete(self, key: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM kv WHERE k = ?", (key,))
            self._db.commit()

    def list_prefix(self, prefix: str, limit: int = 1000) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT k FROM kv WHERE k LIKE ? ESCAPE '\\' ORDER BY k LIMIT ?",
                (prefix.replace("\\", "\\\\").replace("%", "\\%") + "%", limit),
            ).fetchall()
        return [r[0] for r in rows]

    def purge(self) -> int:
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM kv WHERE exp > 0 AND exp <= ?", (self._now(),)
            )
            self._db.commit()
            return cur.rowcount

    # ---- JSON helpers (configs, sessions, buffers) ----

    def get_json(self, key: str, default=None):
        raw = self.get(key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return default

    def put_json(self, key: str, obj, ttl_sec: int = 0) -> None:
        self.put(key, json.dumps(obj, ensure_ascii=False), ttl_sec)

    def close(self) -> None:
        with self._lock:
            self._db.commit()
            self._db.close()
