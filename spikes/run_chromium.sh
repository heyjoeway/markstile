#!/bin/sh
# Launch Chrome or Edge with a managed policy placed at /etc/opt/<vendor>/policies/managed/
# without root: user+mount namespace + overlayfs over /etc, then a nested user namespace
# mapping back to the real uid (Chrome refuses to run as uid 0 with its sandbox).
# Usage: run_chromium.sh chrome|edge BROWSER_BIN POLICY_JSON PROFILE_DIR WORK_DIR [extra chrome flags...]
set -e
KIND=$1; BIN=$2; POLICY=$3; PROFILE=$4; WORK=$5; shift 5
case $KIND in chrome) PDIR=opt/chrome/policies/managed;; edge) PDIR=opt/edge/policies/managed;; *) exit 2;; esac
rm -rf "$WORK/ovl-$KIND"; mkdir -p "$WORK/ovl-$KIND/up/$PDIR" "$WORK/ovl-$KIND/work"
# keep crashpad / config / cache writes out of the real $HOME
export HOME="$WORK/home" XDG_CONFIG_HOME="$WORK/home/.config" XDG_CACHE_HOME="$WORK/home/.cache"
mkdir -p "$HOME"
cp "$POLICY" "$WORK/ovl-$KIND/up/$PDIR/stileaf.json"
UID0=$(id -u) GID0=$(id -g) exec unshare -rm sh -c 'mount -t overlay overlay -o lowerdir=/etc,upperdir="$1",workdir="$2" /etc && shift 2 && exec unshare -U --map-user=$UID0 --map-group=$GID0 "$@"' \
  sh "$WORK/ovl-$KIND/up" "$WORK/ovl-$KIND/work" \
  "$BIN" --user-data-dir="$PROFILE" --no-first-run --no-default-browser-check \
  --password-store=basic --disable-gpu "$@"
