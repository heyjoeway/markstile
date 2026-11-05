"""Entry point: ``markstile run`` (default), ``markstile provision``, ``markstile demo``,
``markstile prefs``."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import tempfile
from pathlib import Path

import uvicorn

from .adapters.base import Registry
from .adapters.ws import ExtensionServer
from .browsers import BROWSERS, ordered
from .portal.app import Context, create_app
from .portal.auth import Auth
from .procs import BrowserProcesses
from .restarts import BrowserRestarter
from .scheduler import Scheduler
from .store import Store
from .sync import Syncer

log = logging.getLogger("markstile")


def parse_interval(value: str | None) -> int | None:
    """'300', '300s', '5m', '1h' -> seconds."""
    if not value:
        return None
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", value)
    if not m:
        raise SystemExit(f"invalid SYNC_INTERVAL: {value!r}")
    return int(float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)])


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


async def serve(demo: bool = False) -> None:
    data = Path(os.environ.get("MARKSTILE_DATA", "/data"))
    run = Path(os.environ.get("MARKSTILE_RUN", "/run/markstile"))
    if demo:
        data = Path(os.environ.get("MARKSTILE_DATA") or tempfile.mkdtemp(prefix="markstile-demo-"))
    store = Store(data / "state.db")
    interval = parse_interval(os.environ.get("SYNC_INTERVAL"))
    if interval and store.get("settings") is None:
        s = store.settings()
        s.sync_interval = interval
        store.save_settings(s)

    auth = Auth(store)
    token = auth.check_reset_env(os.environ.get("PORTAL_RESET"), _env_bool("PORTAL_RESET_TOKEN", True))
    if token is not None:
        msg = "PORTAL_RESET changed: portal password reset is enabled."
        if token:
            msg += f" One-time reset token: {token}"
        print(f"markstile: {msg}", flush=True)
        store.log("warning", "portal", "password reset requested via PORTAL_RESET")

    preset = os.environ.get("MARKSTILE_BROWSERS")
    if preset and store.settings().browsers is None:
        # skip the portal's browser choice (unattended setups)
        chosen = [b.strip() for b in preset.split(",") if b.strip()]
        unknown = [b for b in chosen if b not in BROWSERS]
        if unknown:
            raise SystemExit(f"unknown browser(s) in MARKSTILE_BROWSERS: {', '.join(unknown)}")
        s = store.settings()
        s.browsers = ordered(chosen)
        store.save_settings(s)

    registry = Registry()
    syncer = Syncer(store, registry)
    scheduler = Scheduler(syncer)
    procs = BrowserProcesses(
        None if demo else os.environ.get("SUPERVISORCTL", "supervisorctl -c /etc/markstile/supervisord.conf").split(),
        None if demo else run / "browsers",
        None if demo else data / "profiles",
    )
    for err in await procs.apply(store.settings().browsers):
        store.log("error", "browsers", err)
    registry.on_event = scheduler.on_event


    def on_connect(browser: str, connected: bool) -> None:
        syncer.on_connect(browser, connected)
        if connected:
            asyncio.create_task(syncer.first_contact(browser))
        scheduler.on_connect(browser, connected)

    registry.on_connect = on_connect

    ext_server = None
    if demo:
        await _demo_browsers(registry)
    else:
        secret_file = run / "ext.secret"
        secret = secret_file.read_text().strip() if secret_file.exists() else os.environ.get("MARKSTILE_EXT_SECRET", "")
        if not secret:
            log.warning("no extension secret configured; any local process could act as a browser")
        ext_server = ExtensionServer(
            registry, secret, port=int(os.environ.get("MARKSTILE_WS_PORT", "8765")), ext_dir=run / "ext"
        )
        await ext_server.start()

    ctx = Context(
        store=store, auth=auth, registry=registry, syncer=syncer, scheduler=scheduler, procs=procs,
        vnc_port=int(os.environ.get("MARKSTILE_VNC_PORT", "5901")),
        vnc_password_file=run / "vnc.secret",
        novnc_dir=Path(os.environ.get("NOVNC_DIR", "/usr/share/novnc")),
        secure_cookies=_env_bool("SECURE_COOKIES", False),
    )
    ctx.restarter = BrowserRestarter(store, procs, syncer, ctx.console)
    if ext_server:
        ext_server.on_unconfigured = ctx.restarter.extension_unconfigured
    app = create_app(ctx)
    scheduler.start()
    ctx.restarter.start()
    port = int(os.environ.get("PORT", "34871"))
    host = os.environ.get("BIND", "127.0.0.1" if demo else "0.0.0.0")
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", proxy_headers=True,
                            forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"))
    server = uvicorn.Server(config)
    print(f"markstile: portal on http://{host}:{port}" + (f" (demo data in {data})" if demo else ""), flush=True)
    try:
        await server.serve()
    finally:
        await scheduler.stop()
        await ctx.restarter.stop()
        if ext_server:
            await ext_server.stop()


async def _demo_browsers(registry: Registry) -> None:
    from .adapters.fake import FakeBrowser
    from .engine.model import Node

    B, F = Node.bookmark, Node.folder
    c, e, f = FakeBrowser("chrome"), FakeBrowser("edge"), FakeBrowser("firefox")
    c.add("toolbar", F("Work", [B("Docs", "https://docs.example"), B("Mail", "https://mail.example")]))
    c.add("other", B("Recipes", "https://recipes.example"))
    e.add("toolbar", F("Work", [B("Docs", "https://docs.example")]))
    e.add("mobile", B("Saved on phone", "https://phone.example"))
    f.add("menu", B("Mozilla", "https://www.mozilla.org/"))
    f.add("toolbar", B("News", "https://news.example"))
    for b in (c, e, f):
        await registry.add(b)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "provision":
        from .provision import main as provision_main

        provision_main(sys.argv[2:])
    elif cmd == "prefs":
        from .prefs import main as prefs_main

        prefs_main(sys.argv[2:])
    elif cmd in ("run", "demo"):
        asyncio.run(serve(demo=cmd == "demo"))
    else:
        raise SystemExit("usage: markstile [run|demo|provision ...|prefs ...]")


if __name__ == "__main__":
    main()
