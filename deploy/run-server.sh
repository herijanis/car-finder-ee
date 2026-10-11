#!/bin/sh
# Runs on the Hetzner server from the systemd timers. Argument: "fast" (every 10 min) or "slow" (every 30).
# okidoki, soov and Facebook block server addresses, so the home PC searches those.
set -e
case "$1" in
  fast) SOURCES=autoportaal ;;                                  # cheap: newest-first pages
  slow) SOURCES=autodiiler,veego,easycar,longo,oksjonikeskus ;; # read whole catalogues
  *) echo "usage: run-server.sh fast|slow" >&2; exit 2 ;;
esac
cd /opt/car-finder
git pull -q --ff-only || echo "git pull failed, running the code already here"
export SOURCES SEEN_FILE=seen-server.json SEARCHES_FILE=data/searches.json SEARCH_ALL_DAY=true STATUS_NAME=server
# One run at a time: both timers share seen-server.json.
exec flock /tmp/car-finder.lock .venv/bin/python finder.py
