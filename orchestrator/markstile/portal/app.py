"""The web portal (FastAPI, server-rendered Jinja2 templates)."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..adapters.base import Registry
from ..browsers import BROWSERS, is_gecko, label, ordered
from ..engine.model import ROLES, count_bookmarks
from ..export import to_html
from ..procs import BrowserProcesses
from ..restarts import BrowserRestarter, ConsoleActivity
from ..scheduler import Scheduler
from ..store import Store
from ..sync import Syncer
from .auth import COOKIE, SESSION_TTL, Auth

log = logging.getLogger("markstile.portal")
HERE = Path(__file__).resolve().parent
BROWSER_NAMES = {b.id: b.label for b in BROWSERS.values()}


@dataclass
class Context:
    store: Store
    auth: Auth
    registry: Registry
    syncer: Syncer
    scheduler: Scheduler | None
    procs: BrowserProcesses | None = None
    vnc_host: str = "127.0.0.1"
    vnc_port: int = 5901
    vnc_password_file: Path | None = None
    novnc_dir: Path | None = None
    secure_cookies: bool = False
    console: ConsoleActivity = field(default_factory=ConsoleActivity)
    restarter: BrowserRestarter | None = None


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "never"
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _ago(ts: float | None) -> str:
    if not ts:
        return "never"
    s = int(dt.datetime.now().timestamp() - ts)
    if s < 0:
        return f"in {-s}s" if -s < 120 else f"in {-s // 60} min"
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


def create_app(ctx: Context) -> FastAPI:
    app = FastAPI(title="MarkStile", docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["ts"] = _fmt_ts
    templates.env.filters["ago"] = _ago
    templates.env.filters["label"] = label
    templates.env.globals["BROWSER_NAMES"] = BROWSER_NAMES
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    if ctx.novnc_dir and ctx.novnc_dir.exists():
        app.mount("/novnc", StaticFiles(directory=str(ctx.novnc_dir)), name="novnc")
    app.state.ctx = ctx
    store, auth, syncer = ctx.store, ctx.auth, ctx.syncer

    # -- helpers -------------------------------------------------------------------
    def session(request: Request) -> dict | None:
        return auth.session(request.cookies.get(COOKIE))

    def render(request: Request, name: str, **kw) -> HTMLResponse:
        sess = session(request)
        kw.setdefault("csrf", sess["csrf"] if sess else "")
        kw.setdefault("authed", sess is not None)
        kw.setdefault("sync_enabled", store.settings().sync_enabled)
        kw.setdefault("holds", [b for b in store.browsers() if b.hold])
        kw["page"] = name.removesuffix(".html")
        return templates.TemplateResponse(request, name, kw)

    def guard(request: Request) -> Response | None:
        if auth.needs_setup():
            return RedirectResponse("/setup", 303)
        if session(request) is None:
            return RedirectResponse("/login", 303)
        if store.settings().browsers is None and request.url.path not in ("/browsers", "/logout"):
            return RedirectResponse("/browsers", 303)
        return None

    async def guard_post(request: Request) -> Response | None:
        r = guard(request)
        if r:
            return r
        form = await request.form()
        if form.get("csrf") != session(request)["csrf"]:
            return Response("CSRF check failed", 403)
        return None

    def flash_redirect(url: str, msg: str | None = None) -> RedirectResponse:
        r = RedirectResponse(url, 303)
        if msg:
            r.set_cookie("markstile_flash", msg, max_age=30, httponly=True, samesite="strict", secure=ctx.secure_cookies)
        return r

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        flash = request.cookies.get("markstile_flash")
        if flash and resp.status_code == 200 and "text/html" in resp.headers.get("content-type", ""):
            resp.delete_cookie("markstile_flash")
        return resp

    # -- auth pages --------------------------------------------------------------------
    @app.get("/setup", response_class=HTMLResponse)
    async def setup_page(request: Request):
        if not auth.needs_setup():
            return RedirectResponse("/", 303)
        return render(request, "setup.html", reset=auth.has_password(), error=None)

    @app.post("/setup")
    async def setup_post(request: Request, password: str = Form(...), confirm: str = Form(...), token: str = Form("")):
        if not auth.needs_setup():
            return RedirectResponse("/", 303)
        if password != confirm:
            err = "Passwords do not match."
        else:
            err = auth.set_password(password, token)
        if err:
            return render(request, "setup.html", reset=auth.has_password(), error=err)
        store.log("info", "portal", "portal password set")
        resp = RedirectResponse("/", 303)
        _set_session(resp, auth.new_session())
        return resp

    def _set_session(resp: Response, token: str) -> None:
        resp.set_cookie(COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="strict", secure=ctx.secure_cookies)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if auth.needs_setup():
            return RedirectResponse("/setup", 303)
        return render(request, "login.html", error=None)

    @app.post("/login")
    async def login_post(request: Request, password: str = Form(...)):
        ip = request.client.host if request.client else "?"
        if auth.rate_limited(ip):
            return render(request, "login.html", error="Too many attempts. Wait a few minutes.")
        token = auth.login(password, ip)
        if token is None:
            await asyncio.sleep(0.5)
            return render(request, "login.html", error="Wrong password.")
        resp = RedirectResponse("/", 303)
        _set_session(resp, token)
        return resp

    @app.post("/logout")
    async def logout(request: Request):
        if r := await guard_post(request):
            return r
        auth.logout(request.cookies.get(COOKIE))
        resp = RedirectResponse("/login", 303)
        resp.delete_cookie(COOKIE)
        return resp

    # -- dashboard -------------------------------------------------------------------------
    def status() -> dict:
        connected = set(ctx.registry.connected())
        browsers = []
        settings = store.settings()
        for b in settings.browsers or []:
            st = store.browser(b)
            sess = ctx.registry.sessions.get(b)  # paused browsers aren't read yet
            st.ext_version = getattr(sess, "ext_version", None) or st.ext_version
            browsers.append({
                "name": b, "label": BROWSER_NAMES.get(b, b), "connected": b in connected,
                "bookmarks": st.bookmarks, "last_sync": st.last_sync, "last_error": st.last_error,
                "ext_version": st.ext_version, "browser_version": st.browser_version, "hold": st.hold,
                "mode": st.mode, "paused": b in settings.paused,
            })
        cur = store.current_k()
        sch = ctx.scheduler
        return {
            "browsers": browsers,
            "sync_enabled": store.settings().sync_enabled,
            "running": syncer.running,
            "last_cycle": syncer.last_cycle,
            "next_scheduled": sch.next_scheduled if sch else None,
            "k_version": cur[0] if cur else None,
            "k_bookmarks": count_bookmarks(cur[1]) if cur else None,
        }

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        if r := guard(request):
            return r
        return render(request, "dashboard.html", s=status(), flash=request.cookies.get("markstile_flash"))

    @app.get("/api/status")
    async def api_status(request: Request):
        if session(request) is None:
            return JSONResponse({"error": "unauthorized"}, 401)
        return status()

    @app.post("/sync")
    async def sync_now(request: Request):
        if r := await guard_post(request):
            return r
        if ctx.scheduler:
            ctx.scheduler.request("manual")
        else:
            asyncio.create_task(syncer.run_cycle("manual"))
        return flash_redirect("/", "Sync started.")

    @app.post("/holds/{browser}")
    async def hold_action(request: Request, browser: str, action: str = Form(...)):
        if r := await guard_post(request):
            return r
        if action not in ("approve", "reject"):
            return Response("bad action", 400)
        asyncio.create_task(syncer.resolve_hold(browser, action == "approve"))
        return flash_redirect("/", f"Held changes from {BROWSER_NAMES.get(browser, browser)} {action}d.")

    # -- first run ---------------------------------------------------------------------------
    @app.get("/firstrun", response_class=HTMLResponse)
    async def firstrun_page(request: Request):
        if r := guard(request):
            return r
        preview = await syncer.first_run_preview()
        return render(request, "firstrun.html", preview=preview, connected=ctx.registry.connected(),
                      expected=syncer.enabled(), error=None)

    @app.post("/firstrun")
    async def firstrun_post(request: Request, strategy: str = Form(...), source: str = Form(""), confirm: str = Form("")):
        if r := await guard_post(request):
            return r
        if store.settings().sync_enabled:
            return flash_redirect("/", "Sync is already enabled.")
        if confirm != "yes":
            return flash_redirect("/firstrun", "Tick the confirmation box first.")
        try:
            await syncer.first_run(strategy, source or None)
        except ValueError as e:
            return flash_redirect("/firstrun", str(e))
        return flash_redirect("/", "Sync enabled. First merge done.")

    # -- browsers ----------------------------------------------------------------------------
    @app.get("/browsers", response_class=HTMLResponse)
    async def browsers_page(request: Request):
        if r := guard(request):
            return r
        chosen = store.settings().browsers
        return render(request, "browsers.html", specs=list(BROWSERS.values()), chosen=chosen or [],
                      first=chosen is None, error=None, flash=request.cookies.get("markstile_flash"))

    @app.post("/browsers")
    async def browsers_post(request: Request):
        if r := await guard_post(request):
            return r
        form = await request.form()
        chosen = ordered(form.getlist("browser"))
        first = store.settings().browsers is None
        if not chosen:
            return render(request, "browsers.html", specs=list(BROWSERS.values()), chosen=[], first=first,
                          error="Choose at least one browser.", flash=None)
        added, removed = await syncer.set_browsers(chosen)
        errors = await ctx.procs.apply(chosen) if ctx.procs else []
        if removed and ctx.procs:
            erased, erase_errors = await ctx.procs.erase_profiles(removed)
            errors += erase_errors
            for b in erased:
                store.delete(f"firstcontact.{b}")  # a fresh profile gets checked again
                store.update_browser(b, bookmarks=None, last_sync=None)
            if erased:
                store.log("info", "browsers", "erased the profiles of " + ", ".join(label(b) for b in erased))
        for e in errors:
            store.log("error", "browsers", e)
        if ctx.scheduler and store.settings().sync_enabled and (added or removed):
            ctx.scheduler.request("browsers changed")
        if errors:
            return flash_redirect("/browsers", "Saved, but something went wrong starting, stopping or erasing a browser. See Logs.")
        if first:
            return flash_redirect("/", "Browsers chosen. Next, sign in to them in the Console.")
        return flash_redirect("/browsers", "Browsers updated." if added or removed else "No change.")

    @app.post("/browsers/{browser}/join")
    async def browser_join(request: Request, browser: str):
        if r := await guard_post(request):
            return r
        try:
            await syncer.join(browser)
        except ValueError as e:
            return flash_redirect("/", str(e))
        return flash_redirect("/", f"{BROWSER_NAMES.get(browser, browser)} is now synced.")

    # -- history ------------------------------------------------------------------------------
    @app.get("/history", response_class=HTMLResponse)
    async def history(request: Request):
        if r := guard(request):
            return r
        return render(request, "history.html", versions=store.versions(), flash=request.cookies.get("markstile_flash"))

    @app.get("/history/{vid}/export")
    async def export(request: Request, vid: int):
        if r := guard(request):
            return r
        K = store.version(vid)
        if K is None:
            return Response("not found", 404)
        return Response(to_html(K, f"MarkStile version {vid}"), media_type="text/html; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="markstile-v{vid}.html"'})

    @app.post("/history/{vid}/restore")
    async def restore(request: Request, vid: int, confirm: str = Form("")):
        if r := await guard_post(request):
            return r
        if confirm != "yes":
            return flash_redirect("/history", "Restore needs confirmation.")
        try:
            await syncer.restore(vid)
        except ValueError as e:
            return flash_redirect("/history", str(e))
        return flash_redirect("/history", f"Restored version {vid}; browsers reconciled.")

    # -- settings ---------------------------------------------------------------------------------
    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        if r := guard(request):
            return r
        return settings_render(request, store.settings(), error=None, flash=request.cookies.get("markstile_flash"))

    def settings_render(request: Request, s, error: str | None = None, flash: str | None = None,
                        pw_error: str | None = None) -> HTMLResponse:
        last = ctx.restarter.last_restart if ctx.restarter else None
        due = ctx.restarter.due_at() if ctx.restarter else None
        return render(request, "settings.html", st=s, roles=ROLES, error=error, flash=flash, pw_error=pw_error,
                      last_restart=_ago(last) if last else None, next_restart=_fmt_ts(due) if due else None)

    @app.post("/settings")
    async def settings_post(request: Request):
        if r := await guard_post(request):
            return r
        form = await request.form()
        s = store.settings()
        try:
            for role in ROLES:
                mode = form.get(f"mode_{role}", "mirror")
                dest = (form.get(f"dest_{role}") or "").strip().strip("/")
                if mode == "inbox":
                    if not dest or dest.split("/")[0] not in ROLES or dest.split("/")[0] == role:
                        raise ValueError(f"Inbox destination for {role} must start with another root, e.g. toolbar or other/From phone")
                    s.root_modes[role] = dest
                else:
                    s.root_modes[role] = "mirror"
            menu = (form.get("menu_folder") or "").strip().strip("/")
            if not menu.startswith(("other/", "toolbar/")):
                raise ValueError("The menu folder must be inside other/ or toolbar/")
            s.menu_folder = menu
            s.hold_fraction = float(form.get("hold_percent", 20)) / 100
            s.hold_count = int(form.get("hold_count", 50))
            s.sync_interval = max(30, int(float(form.get("sync_interval_min", 5)) * 60))
            s.priority = [p.strip() for p in (form.get("priority") or "").split(",") if p.strip()]
            bad = [p for p in s.priority if p not in BROWSER_NAMES]
            if bad:
                raise ValueError(f"Unknown browser(s) in priority: {', '.join(bad)}")
            s.retention_days = int(form.get("retention_days", 30))
            s.retention_versions = int(form.get("retention_versions", 100))
            s.restart_hours = int(form.get("restart_hours", 24))
            if s.restart_hours < 0:
                raise ValueError("The restart interval can't be negative")
        except ValueError as e:
            return settings_render(request, s, error=str(e))
        if s.menu_folder != store.settings().menu_folder:
            # The old folder would otherwise look like edits in Chrome/Edge:
            # reconcile them to K once instead of merging their trees.
            for st in store.browsers():
                if not is_gecko(st.browser):
                    store.update_browser(st.browser, mode="rejected")
        store.save_settings(s)
        store.log("info", "portal", "settings changed")
        if ctx.scheduler:
            ctx.scheduler.request("settings changed")
        return flash_redirect("/settings", "Settings saved.")

    @app.get("/settings/password")
    async def change_password_page():
        return RedirectResponse("/settings", 303)  # reloading the page after an error

    @app.post("/settings/password")
    async def change_password(request: Request, current: str = Form(""), password: str = Form(""),
                              confirm: str = Form("")):
        if r := await guard_post(request):
            return r
        ip = request.client.host if request.client else "?"
        if password != confirm:
            err = "The new passwords do not match."
        else:
            err = auth.change_password(current, password, ip)
        if err:
            await asyncio.sleep(0.5)
            return settings_render(request, store.settings(), pw_error=err)
        store.log("info", "portal", "portal password changed; other sessions logged out")
        resp = flash_redirect("/settings", "Password changed. Any other sessions were logged out.")
        _set_session(resp, auth.new_session())
        return resp

    # -- logs ----------------------------------------------------------------------------------------
    @app.get("/logs", response_class=HTMLResponse)
    async def logs(request: Request):
        if r := guard(request):
            return r
        return render(request, "logs.html", logs=store.logs())

    # -- console (noVNC) ------------------------------------------------------------------------------
    @app.get("/console", response_class=HTMLResponse)
    async def console(request: Request):
        if r := guard(request):
            return r
        return render(request, "console.html", novnc=bool(ctx.novnc_dir and ctx.novnc_dir.exists()))

    @app.get("/console/credentials")
    async def console_credentials(request: Request):
        if session(request) is None:
            return JSONResponse({"error": "unauthorized"}, 401)
        pw = ""
        if ctx.vnc_password_file and ctx.vnc_password_file.exists():
            pw = ctx.vnc_password_file.read_text().strip()
        return JSONResponse({"password": pw}, headers={"Cache-Control": "no-store"})

    @app.websocket("/console/ws")
    async def console_ws(ws: WebSocket):
        origin = ws.headers.get("origin")
        host = ws.headers.get("host")
        if auth.session(ws.cookies.get(COOKIE)) is None or (origin and urlparse(origin).netloc != host):
            await ws.close(code=1008)
            return
        try:
            reader, writer = await asyncio.open_connection(ctx.vnc_host, ctx.vnc_port)
        except OSError:
            await ws.close(code=1011)
            return
        await ws.accept(subprotocol="binary" if "binary" in ws.scope.get("subprotocols", []) else None)
        ctx.console.opened()

        async def up():
            try:
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        break
                    data = msg.get("bytes") or (msg.get("text") or "").encode()
                    ctx.console.client_message(data)
                    writer.write(data)
                    await writer.drain()
            except (WebSocketDisconnect, ConnectionError):
                pass

        async def down():
            try:
                while data := await reader.read(65536):
                    await ws.send_bytes(data)
            except (WebSocketDisconnect, ConnectionError, RuntimeError):
                pass

        tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in tasks:
            t.cancel()
        writer.close()
        try:
            await ws.close()
        except RuntimeError:
            pass

    return app
