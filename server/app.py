"""Server entrypoint: polling loop + health endpoint.

- Deletes any Telegram webhook on start and polls getUpdates.
- /health on 0.0.0.0:8787 lets the failover watchdog see liveness.
- Hourly: SQLite purge of expired keys.
"""
from __future__ import annotations

import json
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from cf import Cf
from router import Router
from store import Store
from sync import push_snapshot
from tg import Bot, BotError

CONFIG = "/opt/cg-server/config.json"
DB = "/opt/cg-server/data.db"
HEALTH_PORT = 8787


class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps({"ok": True, "ts": int(time.time())}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main() -> None:
    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    store = Store(DB)
    bot = Bot(cfg["bot_token"])
    cf = Cf(cfg.get("cf_token", ""))
    router = Router(bot, store, cf, int(cfg["admin_id"]),
                    cfg.get("cf_token", ""))

    stop = threading.Event()

    def _sig(*a):
        stop.set()
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    srv = HTTPServer(("0.0.0.0", HEALTH_PORT), _Health)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 1},
                     daemon=True).start()

    bot.delete_webhook()  # polling owns the updates; failover sets webhook only when we are down
    try:
        me = bot.call("getMe")
        print(f"up as @{(me or {}).get('username')} admin={cfg['admin_id']}", flush=True)
    except BotError as e:
        print(f"getMe failed: {e}", flush=True)

    offset, last_purge, last_sync = 0, 0, 0
    import queue as _q

    def _poll(out: _q.Queue, off: int) -> None:
        # در thread جدا تا stall شبکه (حتی DNS) حلقه اصلی را قفل نکند
        try:
            out.put(("ok", bot.get_updates(off, poll_sec=30)))
        except Exception as e:
            out.put(("err", e))

    while not stop.is_set():
        box: _q.Queue = _q.Queue(maxsize=1)
        th = threading.Thread(target=_poll, args=(box, offset), daemon=True)
        th.start()
        th.join(timeout=75)
        if th.is_alive():
            print("poll: stall, skip tick", flush=True)
            time.sleep(2)
            continue
        kind, payload = box.get()
        if kind == "err":
            print(f"poll: {payload}", flush=True)
            time.sleep(5)
            continue
        try:
            for u in payload:
                offset = max(offset, int(u.get("update_id", 0)) + 1)
                try:
                    router.handle(u)
                except Exception as e:
                    print(f"handle: {e}", flush=True)
        except Exception as e:
            print(f"dispatch: {e}", flush=True)
        try:
            for r in router.rem_due(int(time.time() * 1000)):
                bot.send(int(cfg["admin_id"]),
                         f'⏰ یادآور\n\n{r.get("text", "—")}')
        except Exception as e:
            print(f"remind: {e}", flush=True)
        if time.time() - last_purge > 3600:
            try:
                store.purge()
            except Exception:
                pass
            last_purge = time.time()
        if (time.time() - last_sync > 600 and cfg.get("cf_api_token")
                and cfg.get("cf_account_id") and cfg.get("fo_namespace_id")):
            try:
                push_snapshot(cfg["cf_api_token"], cfg["cf_account_id"],
                              cfg["fo_namespace_id"],
                              {"cf_token": cfg.get("cf_token", ""),
                               "hz_token": cfg.get("hz_token", "")})
            except Exception:
                pass
            last_sync = time.time()
    srv.shutdown()
    store.close()


if __name__ == "__main__":
    main()
