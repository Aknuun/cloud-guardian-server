# Cloud Guardian — Server

Full on-server bot (Python + SQLite). No KV, no Worker, no relay needed
for normal operation.

## Fresh install (one command, as root)

```bash
curl -sL https://github.com/Aknuun/cloud-guardian-server/archive/refs/heads/main.tar.gz | tar xz && sudo bash cloud-guardian-server-main/install.sh
```

The installer asks for: bot token (@BotFather), admin numeric Telegram
ID, Cloudflare API token (optional), Hetzner API token (optional),
and failover details (optional).

Health endpoint: `http://127.0.0.1:8787/health`
Service: `systemctl status cg-server`

## Failover

If the server goes down, the worker in `failover/` (cron every 5 min)
takes over cloud management (Cloudflare, Hetzner, panels) from the
KV snapshot the server pushes every 10 min, and hands control back
when the server returns.
