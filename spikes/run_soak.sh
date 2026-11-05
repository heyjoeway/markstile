#!/bin/sh
# Spike 3 soak: Chrome + Edge headless with 20 s app-level pings, plus a Chrome control run whose
# server never pings. Usage: run_soak.sh WORK_DIR CHROME_BIN EDGE_BIN DURATION_SECONDS
set -e
HERE=$(cd "$(dirname "$0")" && pwd); W=$1; CHROME=$2; EDGE=$3; DUR=$4
PY=/home/user/stileaf/.venv/bin/python
$PY "$HERE/spike_server.py" --serve "$W/build/serve" --out "$W/out-soak" --ping-interval 20 > "$W/soak-server.log" 2>&1 &
S1=$!
$PY "$HERE/spike_server.py" --serve "$W/build-noping/serve" --out "$W/out-noping" --http-port 8781 --ws-port 8766 --ping-interval 100000 > "$W/noping-server.log" 2>&1 &
S2=$!
sleep 2
timeout $DUR "$HERE/run_chromium.sh" chrome "$CHROME" "$W/policy/policy-mb.json" "$W/prof-chrome-c" "$W" --headless=new > "$W/soak-chrome.log" 2>&1 &
timeout $DUR "$HERE/run_chromium.sh" edge "$EDGE" "$W/policy/policy-edge-mf.json" "$W/prof-edge-c" "$W" --headless=new > "$W/soak-edge.log" 2>&1 &
sleep 1
# control: needs its own overlay dir name, so use a copy of the launcher kind "chrome" with a separate WORK dir
mkdir -p "$W/noping-work"
timeout $DUR "$HERE/run_chromium.sh" chrome "$CHROME" "$W/policy/policy-noping.json" "$W/prof-chrome-noping" "$W/noping-work" --headless=new > "$W/noping-chrome.log" 2>&1 &
sleep $((DUR + 10))
kill $S1 $S2
