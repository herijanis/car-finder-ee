#!/bin/sh
# Runs on the Hetzner server (systemd timers below). Argument: which sites, e.g. "autoportaal".
# okidoki, soov and Facebook block server addresses, so the home PC searches those.
set -e
cd /opt/car-finder
git pull -q --ff-only || echo "git pull failed, running the code already here"
export SOURCES="$1" SEEN_FILE=seen-server.json SEARCHES_FROM_GITHUB=true SEARCH_ALL_DAY=true
# One run at a time: both timers share seen-server.json.
exec flock /tmp/car-finder.lock .venv/bin/python finder.py
