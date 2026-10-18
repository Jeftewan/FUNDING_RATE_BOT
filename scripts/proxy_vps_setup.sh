#!/usr/bin/env bash
# Proxy SOCKS5 con auth (gost) para la salida del bot hacia exchanges.
# Correr como root en un VPS Ubuntu/Debian NUEVO, en un país donde Binance/Bybit/OKX/Bitget
# den servicio (p. ej. São Paulo). Al final imprime EXCHANGE_PROXY_URL para pegar en
# Railway → FUNDING_RATE_BOT → Variables. No la compartas por chat.
#
#   curl -fsSLO https://raw.githubusercontent.com/Jeftewan/FUNDING_RATE_BOT/main/scripts/proxy_vps_setup.sh
#   sudo bash proxy_vps_setup.sh
set -euo pipefail

GOST_VERSION="${GOST_VERSION:-3.0.0}"
PORT="${PORT:-$(shuf -i 20000-60000 -n 1)}"
USER_NAME="basyo"
PASS="$(openssl rand -hex 24)"

apt-get update -qq && apt-get install -y -qq curl ufw openssl ca-certificates >/dev/null

arch="$(dpkg --print-architecture)"   # amd64 | arm64
tmp="$(mktemp -d)"
curl -fsSL "https://github.com/go-gost/gost/releases/download/v${GOST_VERSION}/gost_${GOST_VERSION}_linux_${arch}.tar.gz" \
  | tar -xz -C "$tmp"
install -m 0755 "$tmp/gost" /usr/local/bin/gost
rm -rf "$tmp"

useradd --system --no-create-home --shell /usr/sbin/nologin gost 2>/dev/null || true
install -d -m 0750 -o root -g gost /etc/gost
cat > /etc/gost/env <<EOF
GOST_LISTEN=socks5://${USER_NAME}:${PASS}@:${PORT}
EOF
chmod 0640 /etc/gost/env; chown root:gost /etc/gost/env

cat > /etc/systemd/system/gost.service <<'EOF'
[Unit]
Description=gost SOCKS5 (salida a exchanges del bot)
After=network-online.target
Wants=network-online.target

[Service]
User=gost
EnvironmentFile=/etc/gost/env
ExecStart=/usr/local/bin/gost -L ${GOST_LISTEN}
Restart=always
RestartSec=3
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow OpenSSH >/dev/null
ufw allow "${PORT}/tcp" >/dev/null
ufw --force enable >/dev/null

systemctl daemon-reload
systemctl enable --now gost >/dev/null
sleep 1
systemctl is-active --quiet gost || { journalctl -u gost -n 20 --no-pager; exit 1; }

IP="$(curl -fsS https://api.ipify.org)"
echo
echo "Salida del VPS: $(curl -fsS https://ipinfo.io/country) ${IP}"
echo "Prueba local:   curl -s -x socks5h://${USER_NAME}:${PASS}@127.0.0.1:${PORT} https://api.binance.com/api/v3/ping"
echo
echo "== Pega esto en Railway (FUNDING_RATE_BOT → Variables) =="
echo "EXCHANGE_PROXY_URL=socks5h://${USER_NAME}:${PASS}@${IP}:${PORT}"
echo
echo "IP para la whitelist de tu API key de Binance: ${IP}"
