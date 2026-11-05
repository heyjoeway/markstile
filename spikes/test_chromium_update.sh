#!/bin/sh
# Spike 1b: can the orchestrator push a new CRX version on demand?
# Installs version A via policy, then serves version B and has the extension call
# runtime.requestUpdateCheck() (onUpdateAvailable -> runtime.reload()).
# Usage: test_chromium_update.sh chrome|edge BROWSER_BIN WORK_DIR   (WORK_DIR/build must exist from build.sh)
HERE=$(cd "$(dirname "$0")" && pwd); KIND=$1; BIN=$2; W=$3
PY=/home/user/stileaf/.venv/bin/python; OUT=$W/out-update-$KIND; B=$W/build
rm -rf "$OUT" "$W/prof-upd-$KIND"; mkdir -p "$OUT"
VER_A=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['version'])" "$B/src-chrome/manifest.json")
VER_B=$(python3 -c "import sys;v=sys.argv[1].split('.');v[-1]=str(int(v[-1])+1);print('.'.join(v))" "$VER_A")
$PY "$HERE/spike_server.py" --serve "$B/serve" --out "$OUT" > /dev/null 2>&1 & SV=$!
timeout 120 "$HERE/run_chromium.sh" $KIND "$BIN" "$W/policy/policy.json" "$W/prof-upd-$KIND" "$W/upd-$KIND" --headless=new > "$OUT/browser.log" 2>&1 &
mkdir -p "$W/upd-$KIND"
i=0; until grep -q '"hello"' "$OUT/events.log" 2>/dev/null || [ $i -ge 40 ]; do sleep 1; i=$((i+1)); done
kill $SV; sleep 1
sed -i "s/\"version\": \"$VER_A\"/\"version\": \"$VER_B\"/" "$B/src-chrome/manifest.json"
python3 -I "$HERE/crx3.py" pack "$B/src-chrome" "$B/key.pem" "$B/serve/stileaf.crx" > /dev/null
python3 -I "$HERE/crx3.py" update-xml "$(cat "$B/ext-id")" "$VER_B" http://127.0.0.1:8780/stileaf.crx > "$B/serve/update.xml"
$PY "$HERE/spike_server.py" --serve "$B/serve" --out "$OUT" --script "$HERE/script-update-check.json" > /dev/null 2>&1 & SV=$!
i=0; until grep -q "\"extVersion\": \"$VER_B\"" "$OUT/events.log" || [ $i -ge 60 ]; do sleep 1; i=$((i+1)); done
sleep 2; kill $SV; pkill -f "[p]rof-upd-$KIND"
grep -E '"hello"|"http"|script done' "$OUT/events.log" | cut -c1-200
grep -o '"update": {[^}]*}' "$OUT"/tree-*-after.json 2>/dev/null; python3 -c "import json,sys;[print(r.get('update')) for r in json.load(open(sys.argv[1]))['results']]" "$OUT/tree-$KIND-after.json"
