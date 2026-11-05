#!/bin/bash
# Container start: secrets, extension/policy provisioning, then supervisord.
set -euo pipefail

RUN_DIR=${MARKSTILE_RUN:-/run/markstile}
DATA=${MARKSTILE_DATA:-/data}
U=${MARKSTILE_USER:-markstile}

install -d -m 0700 -o "$U" -g "$U" "$RUN_DIR"
install -d -o "$U" -g "$U" "$DATA" "$DATA/home" "$DATA/profiles" \
    "$DATA"/profiles/{chrome,edge,brave,vivaldi,opera,firefox}

# VNC: random password at every start, known only to the orchestrator.
VNC_PW=$(head -c 64 /dev/urandom | tr -dc 'A-Za-z0-9' | head -c 8)
printf '%s' "$VNC_PW" > "$RUN_DIR/vnc.secret"
printf '%s\n' "$VNC_PW" | vncpasswd -f > "$RUN_DIR/vncpasswd"
chown "$U:$U" "$RUN_DIR/vnc.secret" "$RUN_DIR/vncpasswd"
chmod 0600 "$RUN_DIR/vnc.secret" "$RUN_DIR/vncpasswd"
unset VNC_PW

# Firefox Developer Edition install directory (policies.json, autoconfig).
if [ -z "${FIREFOX_DIR:-}" ]; then
    FIREFOX_DIR=$(dirname "$(readlink -f "$(command -v firefox-devedition)")")
fi
export FIREFOX_DIR

/opt/markstile/venv/bin/markstile provision --owner "$U" --firefox-dir "$FIREFOX_DIR"

# Browser profile locks name the previous container's hostname; nothing is
# running yet, so they are stale.
rm -f "$DATA"/profiles/{chrome,edge,brave,vivaldi,opera}/Singleton* \
    "$DATA"/profiles/firefox/lock "$DATA"/profiles/firefox/.parentlock

# X socket directory; stale locks from an unclean shutdown.
install -d -m 1777 /tmp/.X11-unix
rm -f /tmp/.X1-lock /tmp/.X11-unix/X1

# Chromium sandbox needs user namespaces; fall back to --no-sandbox only when
# the container runtime does not provide them.
if su -s /bin/sh "$U" -c 'unshare -U true' 2>/dev/null; then
    echo "" > "$RUN_DIR/chromium-flags"
else
    echo "markstile: user namespaces unavailable; Chromium browsers run with --no-sandbox" >&2
    echo "--no-sandbox" > "$RUN_DIR/chromium-flags"
fi
chown "$U:$U" "$RUN_DIR/chromium-flags"

exec /usr/bin/supervisord -n -c /etc/markstile/supervisord.conf
