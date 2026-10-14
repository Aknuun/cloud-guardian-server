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
echo "(هر کدام که از قبل در محیط ست شده باشد پرسیده نمی‌شود)"
BOT_TOKEN="${CG_BOT_TOKEN:-}"
ADMIN_ID="${CG_ADMIN_ID:-}"
CF_TOKEN="${CG_CF_TOKEN:-}"
HZ_TOKEN="${CG_HZ_TOKEN:-}"
CF_ACCOUNT="${CG_CF_ACCOUNT:-}"
FO_NS="${CG_FO_NS:-}"
CF_API_TOKEN="${CG_CF_API_TOKEN:-}"
if [[ "${CG_NONINTERACTIVE:-}" == "1" ]]; then
  [[ -n "$BOT_TOKEN" ]] || { echo "CG_BOT_TOKEN required"; exit 1; }
  [[ -n "$ADMIN_ID" ]] || { echo "CG_ADMIN_ID required"; exit 1; }
else
[[ -n "$BOT_TOKEN" ]] || read -rp "Bot token (از @BotFather بگیرید و اینجا بدهید): " BOT_TOKEN
[[ -n "$ADMIN_ID" ]] || read -rp "Admin numeric Telegram ID (آیدی عددی ادمین): " ADMIN_ID
[[ -n "$CF_TOKEN" ]] || read -rp "Cloudflare API token (Enter to skip): " CF_TOKEN
[[ -n "$HZ_TOKEN" ]] || read -rp "Hetzner API token (Enter to skip): " HZ_TOKEN
[[ -n "$CF_ACCOUNT" ]] || read -rp "Failover: CF account ID (Enter to skip failover): " CF_ACCOUNT
[[ -n "$FO_NS" ]] || read -rp "Failover: KV namespace ID: " FO_NS
[[ -n "$CF_API_TOKEN" ]] || read -rp "Failover: CF API token with KV+Workers rights: " CF_API_TOKEN
fi

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
TimeoutStopSec=15
UMask=0077
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now cg-server
if command -v ufw >/dev/null; then ufw allow 8787/tcp >/dev/null 2>&1 || true; fi

echo "installed. status:"
systemctl --no-pager status cg-server | head -8
echo "health: curl http://127.0.0.1:8787/health"
