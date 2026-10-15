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
    store = None
    bot = None
    admin_id = 0
    router = None

    def _json(self, code: int, obj) -> None:
        import json as _j
        body = _j.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        self._json(200, {"ok": True, "ts": int(time.time())})

    def do_POST(self):
        # وبهوک هشدار پنل: /pghook/<secret>
        if not self.path.startswith("/pghook/"):
            self.send_response(404)
            self.end_headers()
            return
        try:
            ln = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(max(0, min(ln, 1 << 20))).decode(
                "utf-8", "replace")
            import json as _j
            payload = _j.loads(raw) if raw else {}
        except Exception:
            return self._json(400, {"ok": False})
        try:
            import alerts as _al
            secret = self.path.rsplit("/", 1)[-1]
            panels = (self.router._pg_panels()
                      if self.router else [])
            cfg = _al.get_cfg(self.store)
            hit = None
            for p in panels:
                pc = (cfg.get("panels") or {}).get(str(p.get("url"))) or {}
                if pc.get("secret") == secret:
                    hit = p
                    break
            if hit is None:
                return self._json(403, {"ok": False})
            n = _al.handle_events(self.bot, self.store, hit,
                                  str(hit.get("url")), payload,
                                  self.admin_id)
            return self._json(200, {"ok": True, "buffered": n})
        except Exception as e:
            print(f"pghook: {e}", flush=True)
            return self._json(500, {"ok": False})

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
    _Health.store = store
    _Health.bot = bot
    _Health.admin_id = int(cfg["admin_id"])
    _Health.router = router
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 1},
                     daemon=True).start()

    bot.delete_webhook()  # polling owns the updates; failover sets webhook only when we are down
    try:
        me = bot.call("getMe")
        print(f"up as @{(me or {}).get('username')} admin={cfg['admin_id']}", flush=True)
    except BotError as e:
        print(f"getMe failed: {e}", flush=True)

    offset, last_purge, last_sync = 0, 0, 0
    _boot = time.time()
    try:
        _meta = store.get_json("sched_meta", {}) or {}
    except Exception:
        _meta = {}
    last_flush = float(_meta.get("flush", _boot))
    last_digest_u = float(_meta.get("digest_u", _boot))
    last_digest_d = float(_meta.get("digest_d", _boot))
    last_domexp = float(_meta.get("domexp", _boot))
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
        try:
            import alerts as _al
            from monitors import run_usage_monitor, run_node_poll
            from pg import Pg as _Pg
            now = time.time()
            panels = router._pg_panels()
            pmap = {str(p.get("url")): p for p in panels}
            if now - last_flush > 60:
                _al.flush_buffers(bot, store, pmap, int(cfg["admin_id"]))
                last_flush = now
                _pgc = lambda p: _Pg(p["url"], p["username"], p["password"])
                try:
                    run_usage_monitor(bot, store, panels, int(cfg["admin_id"]),
                                      _pgc)
                except Exception as e:
                    print(f"um: {e}", flush=True)
                try:
                    run_node_poll(bot, store, panels, int(cfg["admin_id"]),
                                  _pgc)
                except Exception as e:
                    print(f"node: {e}", flush=True)
                try:
                    from monitors import run_srvmon
                    from sshop import stats as _stats
                    run_srvmon(bot, store,
                               router._srv_list(), int(cfg["admin_id"]),
                               lambda s: _stats(
                                   s.get("host"), s.get("user") or "root",
                                   s.get("secret") or "",
                                   s.get("port") or 22))
                except Exception as e:
                    print(f"srvmon: {e}", flush=True)
            if now - _meta.get("hf", 0) > 60:
                _meta["hf"] = now
                try:
                    import threading as _th2
                    from hostfilter import run as _hfrun
                    _th2.Thread(target=_hfrun,
                                args=(bot, store, router._pg_panels(),
                                      int(cfg["admin_id"]),
                                      router._cf_accs(), _pgc),
                                daemon=True).start()
                except Exception as e:
                    print(f"hf: {e}", flush=True)
            if now - last_digest_u > 3600:
                _al.run_digest(bot, store, panels, "usage",
                               int(cfg["admin_id"]),
                               lambda p: _Pg(p["url"], p["username"],
                                             p["password"]))
                last_digest_u = now
            if now - last_digest_d > 86400:
                _al.run_digest(bot, store, panels, "days",
                               int(cfg["admin_id"]),
                               lambda p: _Pg(p["url"], p["username"],
                                              p["password"]))
                last_digest_d = now
            if now - last_domexp > 86400:
                try:
                    from monitors import run_domexp
                    run_domexp(bot, store, router, int(cfg["admin_id"]))
                except Exception as e:
                    print(f"domexp: {e}", flush=True)
                last_domexp = now
            try:
                store.put_json("sched_meta",
                               {"flush": last_flush,
                                "digest_u": last_digest_u,
                                "digest_d": last_digest_d,
                                "domexp": last_domexp})
            except Exception:
                pass
        except Exception as e:
            print(f"alerts: {e}", flush=True)
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
