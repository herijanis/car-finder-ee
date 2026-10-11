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
