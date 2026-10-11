#!/bin/sh
# Runs on the Hetzner server from the systemd timers. Argument: "fast" (every 5 min) or "slow" (every 15).
# okidoki, soov and Facebook block server addresses, so the home PC searches those.
set -e
case "$1" in
  fast) SOURCES=autoportaal,veego,longo,oksjonikeskus ;;  # filtered by model or small: a few requests each
  slow) SOURCES=autodiiler,easycar ;;                     # no filters: whole catalogues, ~100 pages each
  *) echo "usage: run-server.sh fast|slow" >&2; exit 2 ;;
esac
cd /opt/car-finder
git pull -q --ff-only || echo "git pull failed, running the code already here"
export SOURCES SEEN_FILE=seen-server.json SEARCHES_FILE=data/searches.json SEARCH_ALL_DAY=true STATUS_NAME=server
# One run at a time: both timers share seen-server.json.
exec flock /tmp/car-finder.lock .venv/bin/python finder.py
