#!/bin/sh
# Spike 4: force-install an unsigned XPI into Firefox Developer Edition.
# Usage: run_firefox.sh MODE FF_INSTALL_DIR XPI PROFILE_DIR WORK_DIR SECONDS
#   MODE = none | userjs | autoconfig | both  (how xpinstall.signatures.required=false is set)
# Writes FF_INSTALL_DIR/distribution/policies.json and (for autoconfig) defaults/pref/autoconfig.js +
# mozilla.cfg; removes them for the other modes. Runs headless for SECONDS.
set -e
MODE=$1; FF=$2; XPI=$3; PROFILE=$4; WORK=$5; SECS=$6
mkdir -p "$FF/distribution" "$PROFILE" "$WORK/home"
cat > "$FF/distribution/policies.json" <<JSON
{
  "policies": {
    "ExtensionSettings": {
      "spike@stileaf.local": {
        "installation_mode": "force_installed",
        "install_url": "file://$XPI"
      }
    }
  }
}
JSON
rm -f "$FF/defaults/pref/autoconfig.js" "$FF/mozilla.cfg" "$PROFILE/user.js"
case $MODE in autoconfig|both)
  cat > "$FF/defaults/pref/autoconfig.js" <<'JS'
pref("general.config.filename", "mozilla.cfg");
pref("general.config.obscure_value", 0);
JS
  cat > "$FF/mozilla.cfg" <<'CFG'
// mozilla.cfg: first line must be a comment
lockPref("xpinstall.signatures.required", false);
CFG
;; esac
case $MODE in userjs|both)
  echo 'user_pref("xpinstall.signatures.required", false);' > "$PROFILE/user.js"
;; esac
export HOME="$WORK/home" XDG_CONFIG_HOME="$WORK/home/.config" XDG_CACHE_HOME="$WORK/home/.cache" MOZ_CRASHREPORTER_DISABLE=1
exec timeout "$SECS" "$FF/firefox" --headless --no-remote --profile "$PROFILE"
