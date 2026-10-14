#!/usr/bin/env bash
# Cloud Guardian — server install (fresh).
# Usage: sudo bash install.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then echo "run as root"; exit 1; fi
command -v python3 >/dev/null || { echo "python3 needed"; exit 1; }

SRC="$(cd "$(dirname "$0")" && pwd)"
DST="/opt/cg-server"
mkdir -p "$DST"
cp "$SRC/server/"*.py "$DST/"
chmod 600 "$DST"/*.py 2>/dev/null || true

echo "== Cloud Guardian server setup =="
read -rp "Bot token (from @BotFather): " BOT_TOKEN
read -rp "Admin numeric Telegram ID: " ADMIN_ID
read -rp "Cloudflare API token (Enter to skip): " CF_TOKEN
read -rp "Hetzner API token (Enter to skip): " HZ_TOKEN
read -rp "Failover: CF account ID (Enter to skip failover): " CF_ACCOUNT
read -rp "Failover: KV namespace ID: " FO_NS
read -rp "Failover: CF API token with KV+Workers rights: " CF_API_TOKEN

cat > "$DST/config.json" <<EOF
{
  "bot_token": "$BOT_TOKEN",
  "admin_id": $ADMIN_ID,
  "cf_token": "$CF_TOKEN",
  "hz_token": "$HZ_TOKEN",
  "cf_account_id": "$CF_ACCOUNT",
  "fo_namespace_id": "$FO_NS",
  "cf_api_token": "$CF_API_TOKEN"
}
EOF
chmod 600 "$DST/config.json"

cat > /etc/systemd/system/cg-server.service <<EOF
[Unit]
Description=Cloud Guardian server bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$DST
ExecStart=/usr/bin/python3 $DST/app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now cg-server
if command -v ufw >/dev/null; then ufw allow 8787/tcp >/dev/null 2>&1 || true; fi

echo "installed. status:"
systemctl --no-pager status cg-server | head -8
echo "health: curl http://127.0.0.1:8787/health"
