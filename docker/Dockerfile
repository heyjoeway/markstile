# MarkStile: real Chrome, Edge, Brave, Vivaldi, Opera and Firefox Developer Edition as
# bookmark-sync adapters.  x86-64 only (the Chromium browsers ship x64-only Linux builds).
# Build from the repository root:  docker build -f docker/Dockerfile -t markstile .
FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg tzdata procps tini util-linux \
        python3 python3-venv \
        supervisor tigervnc-standalone-server tigervnc-tools openbox novnc \
        dbus-x11 xdg-utils fonts-noto-core fonts-noto-color-emoji fonts-liberation \
    && install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /etc/apt/keyrings/google.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /etc/apt/keyrings/microsoft.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/edge stable main" \
        > /etc/apt/sources.list.d/microsoft-edge.list \
    && curl -fsSL https://packages.mozilla.org/apt/repo-signing-key.gpg -o /etc/apt/keyrings/packages.mozilla.org.asc \
    && echo "deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main" \
        > /etc/apt/sources.list.d/mozilla.list \
    && printf 'Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n' > /etc/apt/preferences.d/mozilla \
    && apt-get update && apt-get install -y --no-install-recommends \
        google-chrome-stable microsoft-edge-stable firefox-devedition \
    && rm -rf /var/lib/apt/lists/*

# Brave (its own layer, so adding it did not invalidate the layer above).
RUN curl -fsSL https://brave-browser-apt-release.s3.brave.com/brave-browser-archive-keyring.gpg \
        -o /etc/apt/keyrings/brave-browser-archive-keyring.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/brave-browser-archive-keyring.gpg] https://brave-browser-apt-release.s3.brave.com/ stable main" \
        > /etc/apt/sources.list.d/brave-browser-release.list \
    && apt-get update && apt-get install -y --no-install-recommends brave-browser \
    && rm -rf /var/lib/apt/lists/*

# Vivaldi and Opera (own layer for the same reason).  Vivaldi fetches its media
# codecs here once instead of trying at every launch.
RUN curl -fsSL https://repo.vivaldi.com/archive/linux_signing_key.pub | gpg --dearmor -o /etc/apt/keyrings/vivaldi.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/vivaldi.gpg] https://repo.vivaldi.com/archive/deb/ stable main" \
        > /etc/apt/sources.list.d/vivaldi.list \
    && curl -fsSL https://deb.opera.com/archive.key | gpg --dearmor -o /etc/apt/keyrings/opera.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/opera.gpg] https://deb.opera.com/opera-stable/ stable non-free" \
        > /etc/apt/sources.list.d/opera-stable.list \
    && apt-get update && apt-get install -y --no-install-recommends xz-utils gsettings-desktop-schemas vivaldi-stable opera-stable \
    # Vivaldi's postinst starts update-ffmpeg in the background; running a second
    # copy alongside it fails at random, so wait for it (ours then finds the
    # codecs in place) and retry once if the download itself failed
    && for _ in $(seq 120); do pgrep -x update-ffmpeg >/dev/null || break; sleep 1; done \
    && { /opt/vivaldi/update-ffmpeg --system || /opt/vivaldi/update-ffmpeg --system; } \
    && rm -rf /var/lib/apt/lists/*

# Taskbar for switching between the browser windows in the console.
RUN apt-get update && apt-get install -y --no-install-recommends tint2 \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged user that runs the browsers and the orchestrator.
RUN (userdel -r ubuntu 2>/dev/null || true) && useradd -m -u 1000 -s /bin/bash markstile

COPY orchestrator /src/orchestrator
COPY extension /src/extension
RUN python3 -m venv /opt/markstile/venv \
    && /opt/markstile/venv/bin/pip install --no-cache-dir /src/orchestrator \
    && python3 /src/extension/build.py /opt/markstile/ext

COPY docker/supervisord.conf /etc/markstile/supervisord.conf
COPY docker/openbox /etc/markstile/openbox
COPY docker/bin /opt/markstile/bin
COPY docker/entrypoint.sh /opt/markstile/entrypoint.sh
RUN chmod 0755 /opt/markstile/bin/* /opt/markstile/entrypoint.sh

ENV MARKSTILE_DATA=/data \
    MARKSTILE_RUN=/run/markstile \
    MARKSTILE_EXT=/opt/markstile/ext \
    MARKSTILE_USER=markstile \
    NOVNC_DIR=/usr/share/novnc \
    PORT=8080 \
    LANG=C.UTF-8

VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s \
    CMD curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/login" || exit 1
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/markstile/entrypoint.sh"]
