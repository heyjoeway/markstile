#!/bin/sh
# Build spike packages into $1 (default: ./build). Key is generated once at $1/key.pem.
# Env: WS_PORT (8765), HTTP_PORT (8780), NO_ALARM=1 (control build without the keepalive alarm)
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${1:-$HERE/build}; WS_PORT=${WS_PORT:-8765}; HTTP_PORT=${HTTP_PORT:-8780}
mkdir -p "$OUT/serve"
[ -f "$OUT/key.pem" ] || python3 -I "$HERE/crx3.py" genkey "$OUT/key.pem"
for b in chrome firefox; do
  rm -rf "$OUT/src-$b"; mkdir -p "$OUT/src-$b"
  cp "$HERE/ext/common/"* "$HERE/ext/$b/"* "$OUT/src-$b/"
  echo "self.STILEAF_WS_PORT = $WS_PORT;" > "$OUT/src-$b/config.js"
  if [ -n "$NO_ALARM" ]; then echo "self.STILEAF_NO_ALARM = true;" >> "$OUT/src-$b/config.js"; fi
done
# Chromium: put update_url in the manifest. Without it, later update checks go to the Chrome Web Store
# (update.googleapis.com) instead of the forcelist URL, so self-hosted updates never arrive.
python3 - "$OUT/src-chrome/manifest.json" "http://127.0.0.1:$HTTP_PORT/update.xml" <<'PY'
import json, sys
p, url = sys.argv[1], sys.argv[2]
m = json.load(open(p)); m["update_url"] = url
json.dump(m, open(p, "w"), indent=2)
PY
VER=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['version'])" "$OUT/src-chrome/manifest.json")
ID=$(python3 -I "$HERE/crx3.py" pack "$OUT/src-chrome" "$OUT/key.pem" "$OUT/serve/stileaf.crx")
python3 -I "$HERE/crx3.py" update-xml "$ID" "$VER" "http://127.0.0.1:$HTTP_PORT/stileaf.crx" > "$OUT/serve/update.xml"
rm -f "$OUT/serve/stileaf.xpi"; (cd "$OUT/src-firefox" && zip -qr -X "$OUT/serve/stileaf.xpi" .)
echo "$ID" > "$OUT/ext-id"
echo "extension id: $ID"
