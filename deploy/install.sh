#!/bin/sh
# One-time setup on the server: sh deploy/install.sh (run from /opt/car-finder).
set -e
cd /opt/car-finder
command -v python3 >/dev/null
dpkg -s python3-venv >/dev/null 2>&1 || apt-get install -y -q python3-venv
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
chmod +x deploy/run-server.sh
cp deploy/car-finder@.service deploy/car-finder-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl disable --now car-finder-autoportaal.timer car-finder-autodiiler.timer 2>/dev/null || true
rm -f /etc/systemd/system/car-finder-autoportaal.timer /etc/systemd/system/car-finder-autodiiler.timer
systemctl daemon-reload
systemctl enable --now car-finder-fast.timer car-finder-slow.timer
systemctl list-timers 'car-finder*' --no-pager

# The app and its API (api.py behind Caddy on https). Saving needs the PIN in data/pin.txt.
mkdir -p data
[ -f data/searches.json ] || cp searches.json data/searches.json
[ -f data/pin.txt ] || { shuf -i 1000-9999 -n 1 > data/pin.txt; chmod 600 data/pin.txt; }
if ! command -v caddy >/dev/null; then
  apt-get install -y -q debian-keyring debian-archive-keyring apt-transport-https curl gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q && apt-get install -y -q caddy
fi
cp deploy/Caddyfile /etc/caddy/Caddyfile
cp deploy/car-finder-api.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now car-finder-api.service
systemctl restart car-finder-api.service
systemctl reload caddy || systemctl restart caddy
ufw allow 80/tcp >/dev/null && ufw allow 443/tcp >/dev/null
