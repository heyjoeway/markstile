#!/bin/sh
# Spike 3 controls with an extension build that has NO keepalive alarm (NO_ALARM=1):
#   A: server pings every 20 s (Chrome + Edge)   B: server never pings (Chrome)
# Usage: run_bare_controls.sh WORK_DIR CHROME_BIN EDGE_BIN DURATION_SECONDS
HERE=$(cd "$(dirname "$0")" && pwd); W=$1; CHROME=$2; EDGE=$3; DUR=$4
PY=/home/user/stileaf/.venv/bin/python
$PY "$HERE/spike_server.py" --serve "$W/build-bare/serve" --out "$W/out-bareA" --http-port 8783 --ws-port 8768 --ping-interval 20 > /dev/null 2>&1 &
S1=$!
$PY "$HERE/spike_server.py" --serve "$W/build-bare2/serve" --out "$W/out-bareB" --http-port 8784 --ws-port 8769 --ping-interval 100000 > /dev/null 2>&1 &
S2=$!
sleep 2
mkdir -p "$W/wA" "$W/wB"
timeout $DUR "$HERE/run_chromium.sh" chrome "$CHROME" "$W/policy/policy-bare.json" "$W/prof-bareA-chrome" "$W/wA" --headless=new > "$W/bareA-chrome.log" 2>&1 &
timeout $DUR "$HERE/run_chromium.sh" edge "$EDGE" "$W/policy/policy-bare.json" "$W/prof-bareA-edge" "$W/wA" --headless=new > "$W/bareA-edge.log" 2>&1 &
timeout $DUR "$HERE/run_chromium.sh" chrome "$CHROME" "$W/policy/policy-bare2.json" "$W/prof-bareB-chrome" "$W/wB" --headless=new > "$W/bareB-chrome.log" 2>&1 &
sleep $((DUR + 10))
kill $S1 $S2
