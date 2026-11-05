#!/bin/sh
# Follow-up spike: behaviour of the hidden (empty) mobile root "3" in Chrome/Edge.
# Usage: test_mobile_root.sh chrome|edge BROWSER_BIN WORK_DIR [SCRIPT] [fresh|seed|reuse] [TAG]
#   fresh: new profile (mobile root empty/hidden). seed: new profile, first run once to create it, then
#   write bookmark id 100 into roots.synced of the on-disk Bookmarks file. reuse: keep the profile.
# (WORK_DIR/build from build.sh, WORK_DIR/policy/policy.json)
HERE=$(cd "$(dirname "$0")" && pwd); KIND=$1; BIN=$2; W=$3
SCRIPT=${4:-$HERE/script-mobile-root.json}; MODE=${5:-fresh}; TAG=${6:-$MODE}
PY=/home/user/stileaf/.venv/bin/python; OUT=$W/out-mobile-$KIND-$TAG; PROF=$W/prof-mobile-$KIND
rm -rf "$OUT"; mkdir -p "$OUT" "$W/mobile-$KIND"
if [ "$MODE" != reuse ]; then rm -rf "$PROF"; fi
if [ "$MODE" = seed ]; then
  # first run: create a bookmark so the browser writes Default/Bookmarks, then stop it
  mkdir -p "$OUT/seed"
  $PY "$HERE/spike_server.py" --serve "$W/build/serve" --out "$OUT/seed" --script "$HERE/script-create.json" > /dev/null 2>&1 & SV=$!
  timeout 60 "$HERE/run_chromium.sh" $KIND "$BIN" "$W/policy/policy.json" "$PROF" "$W/mobile-$KIND" --headless=new > /dev/null 2>&1 &
  i=0; until grep -q 'script done' "$OUT/seed/events.log" 2>/dev/null || [ $i -ge 50 ]; do sleep 1; i=$((i+1)); done
  sleep 4; pkill -f "[p]rof-mobile-$KIND"; sleep 2; kill $SV
  python3 -I - "$PROF/Default/Bookmarks" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p)); d.pop("checksum", None)
d["roots"]["synced"]["children"].append({"date_added": "13370000000000000", "guid": "6f0e6a43-1111-4a8e-9a3e-0123456789ab",
    "id": "100", "name": "Seeded mobile bookmark", "type": "url", "url": "https://example.com/mobile"})
json.dump(d, open(p, "w"), indent=3)
PY
fi
$PY "$HERE/spike_server.py" --serve "$W/build/serve" --out "$OUT" --script "$SCRIPT" > /dev/null 2>&1 & SV=$!
timeout 60 "$HERE/run_chromium.sh" $KIND "$BIN" "$W/policy/policy.json" "$PROF" "$W/mobile-$KIND" --headless=new > "$OUT/browser.log" 2>&1 &
i=0; until grep -q 'script done' "$OUT/events.log" 2>/dev/null || [ $i -ge 50 ]; do sleep 1; i=$((i+1)); done
sleep 5   # Chromium writes the Bookmarks file ~2.5 s after the last change; let it flush before stopping
pkill -f "[p]rof-mobile-$KIND"; sleep 2; kill $SV
python3 -I - "$OUT/tree-$KIND-after.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
for i, (step, r) in enumerate(zip(d["steps"], d["results"])):
    label = step.get("method", step["type"]) + (" " + json.dumps(step.get("args")) if "args" in step else "")
    if step["type"] == "getTree":
        roots = [(c["id"], c.get("folderType"), len(c.get("children", []))) for c in r["tree"][0]["children"]]
        print(f"{i:2} getTree -> roots (id, folderType, #children): {roots}")
    elif r.get("ok"):
        v = r.get("value")
        if isinstance(v, list):
            v = [{k: x.get(k) for k in ("id", "parentId", "title", "folderType", "index") if k in x} | ({"children": len(x["children"])} if "children" in x else {}) for x in v]
        elif isinstance(v, dict):
            v = {k: v.get(k) for k in ("id", "parentId", "title", "folderType", "index") if k in v}
        print(f"{i:2} {label[:90]} -> OK {json.dumps(v)}")
    else:
        print(f"{i:2} {label[:90]} -> ERROR {r.get('error')}")
PY
