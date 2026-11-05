import re

import pytest
from fastapi.testclient import TestClient

from markstile.adapters.base import Registry
from markstile.adapters.fake import FakeBrowser
from markstile.engine.model import Node
from markstile.portal.app import Context, create_app
from markstile.portal.auth import Auth
from markstile.procs import BrowserProcesses
from markstile.store import Store
from markstile.sync import Syncer

PW = "correct horse battery"


@pytest.fixture
def env():
    store = Store(":memory:")
    reg = Registry()
    fakes = {n: FakeBrowser(n) for n in ("chrome", "edge", "firefox")}
    fakes["chrome"].add("toolbar", Node.bookmark("a", "http://a"))
    for f in fakes.values():
        reg.sessions[f.browser] = f
    auth = Auth(store)
    ctx = Context(store, auth, reg, Syncer(store, reg), None, BrowserProcesses())
    client = TestClient(create_app(ctx), base_url="http://testserver")
    client.procs = ctx.procs
    return client, store, auth, fakes


def csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def setup_pw(client, browsers=("chrome", "edge", "firefox")):
    r = client.post("/setup", data={"password": PW, "confirm": PW})
    assert r.status_code == 200 and r.url.path == "/browsers" and "Choose your browsers" in r.text
    r = client.post("/browsers", data={"csrf": csrf(r.text), "browser": list(browsers)})
    assert r.status_code == 200 and "Dashboard" in r.text


def test_first_visitor_sets_password_then_login(env):
    client, store, auth, _ = env
    assert client.get("/").url.path == "/setup"
    assert "at least" in client.post("/setup", data={"password": "short", "confirm": "short"}).text
    setup_pw(client)
    # setup is closed now
    assert client.get("/setup").url.path == "/"
    client.cookies.clear()
    assert client.get("/").url.path == "/login"
    assert "Wrong password" in client.post("/login", data={"password": "nope"}).text
    assert "Dashboard" in client.post("/login", data={"password": PW}).text


def test_log_out_from_settings(env):
    client, *_ = env
    setup_pw(client)
    assert 'action="/logout"' not in client.get("/").text
    page = client.get("/settings")
    assert 'id="logout-form"' in page.text and 'id="logout-dialog"' in page.text
    r = client.post("/logout", data={"csrf": csrf(page.text)})
    assert r.url.path == "/login"
    assert client.get("/settings").url.path == "/login"


def test_change_password(env):
    client, store, auth, _ = env
    setup_pw(client)
    other = auth.new_session()
    page = client.get("/settings")
    new = "a whole new password"

    def change(current, password, confirm):
        return client.post("/settings/password", data={
            "csrf": csrf(page.text), "current": current, "password": password, "confirm": confirm})

    assert "current password is wrong" in change("nope", new, new).text
    assert "do not match" in change(PW, new, new + "x").text
    assert "at least" in change(PW, "short", "short").text
    r = change(PW, new, new)
    assert r.url.path == "/settings" and "Password changed" in r.text
    assert auth.session(other) is None  # other sessions are logged out
    assert client.get("/").url.path == "/"  # but not this one
    client.cookies.clear()
    assert "Wrong password" in client.post("/login", data={"password": PW}).text
    assert "Dashboard" in client.post("/login", data={"password": new}).text


def test_restart_interval_setting(env):
    client, store, *_ = env
    setup_pw(client)
    page = client.get("/settings")
    assert 'name="restart_hours" min="0" value="24"' in page.text
    form = {"csrf": csrf(page.text), "mode_toolbar": "mirror", "mode_other": "mirror", "mode_mobile": "mirror",
            "mode_menu": "mirror", "menu_folder": "other/Bookmarks Menu", "restart_hours": "6"}
    client.post("/settings", data=form)
    assert store.settings().restart_hours == 6
    assert "negative" in client.post("/settings", data={**form, "restart_hours": "-1"}).text


def test_removing_a_browser_erases_its_profile(env, tmp_path):
    client, store, *_ = env
    procs = client.procs
    procs.supervisorctl, procs.profiles = ["true"], tmp_path
    for b in ("chrome", "edge", "firefox"):
        (tmp_path / b / "Default").mkdir(parents=True)
        (tmp_path / b / "Default" / "Bookmarks").write_text("{}")
    setup_pw(client)
    store.set("firstcontact.firefox", 1.0)
    page = client.get("/browsers")
    assert 'id="remove-dialog"' in page.text and "erases its profile" in page.text
    r = client.post("/browsers", data={"csrf": csrf(page.text), "browser": ["chrome"]})
    assert "Browsers updated" in r.text
    assert (tmp_path / "chrome" / "Default" / "Bookmarks").exists()
    assert list((tmp_path / "edge").iterdir()) == []
    assert [p.name for p in (tmp_path / "firefox").iterdir()] == ["user.js"]
    assert store.get("firstcontact.firefox") is None
    assert "erased the profiles of Microsoft Edge, Firefox Developer Edition" in store.logs()[0]["message"]


def test_login_rate_limit(env):
    client, *_ = env
    setup_pw(client)
    client.cookies.clear()
    for _ in range(5):
        client.post("/login", data={"password": "nope"})
    assert "Too many" in client.post("/login", data={"password": PW}).text


def test_csrf_required(env):
    client, *_ = env
    setup_pw(client)
    assert client.post("/sync", data={"csrf": "bad"}).status_code == 403


def test_reset_flow_requires_token(env):
    client, store, auth, _ = env
    setup_pw(client)
    assert auth.check_reset_env("") is None
    token = auth.check_reset_env("2024-01")
    assert token and auth.check_reset_env("2024-01") is None  # same value: no new reset
    # existing sessions are locked out during reset
    assert client.get("/").url.path == "/setup"
    new = "another long password"
    r = client.post("/setup", data={"password": new, "confirm": new, "token": "WRONG"})
    assert "Wrong reset token" in r.text
    r = client.post("/setup", data={"password": new, "confirm": new, "token": token.lower()})
    assert "Dashboard" in r.text
    client.cookies.clear()
    assert "Dashboard" in client.post("/login", data={"password": new}).text


def test_firstrun_settings_history_export(env):
    client, store, auth, fakes = env
    setup_pw(client)
    page = client.get("/firstrun")
    assert "Union" in page.text and "Google Chrome" in page.text
    r = client.post("/firstrun", data={"csrf": csrf(page.text), "strategy": "union", "confirm": "yes"})
    assert "Sync enabled" in r.text
    assert [c.title for c in fakes["edge"].tree["toolbar"].children] == ["a"]
    page = client.get("/settings")
    r = client.post("/settings", data={
        "csrf": csrf(page.text), "mode_toolbar": "mirror", "mode_other": "mirror", "mode_menu": "mirror",
        "mode_mobile": "inbox", "dest_mobile": "other/From phone", "menu_folder": "other/Bookmarks Menu",
        "hold_percent": "25", "hold_count": "40", "sync_interval_min": "10", "priority": "firefox, chrome",
        "retention_days": "30", "retention_versions": "100"})
    assert "Settings saved" in r.text
    s = store.settings()
    assert s.root_modes["mobile"] == "other/From phone" and s.hold_fraction == 0.25 and s.sync_interval == 600
    bad = client.post("/settings", data={"csrf": csrf(page.text), "mode_mobile": "inbox", "dest_mobile": "mobile/x",
                                          "menu_folder": "other/M"})
    assert "must start with another root" in bad.text
    hist = client.get("/history")
    assert "first run: union" in hist.text
    vid = store.versions()[0]["id"]
    exp = client.get(f"/history/{vid}/export")
    assert exp.headers["content-disposition"].startswith("attachment")
    assert '<A HREF="http://a">a</A>' in exp.text
    assert client.get("/logs").status_code == 200
    assert client.get("/api/status").json()["sync_enabled"] is True


def test_console_ws_requires_session(env):
    client, *_ = env
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/console/ws") as ws:
            ws.receive_bytes()


def test_browser_choice(env):
    client, store, auth, fakes = env
    r = client.post("/setup", data={"password": PW, "confirm": PW})
    # every page leads to the choice until it is made
    assert client.get("/settings").url.path == "/browsers"
    assert "at least one" in client.post("/browsers", data={"csrf": csrf(r.text)}).text
    r = client.post("/browsers", data={"csrf": csrf(r.text), "browser": ["firefox", "chrome"]})
    assert r.url.path == "/" and "Browsers chosen" in r.text
    assert store.settings().browsers == ["chrome", "firefox"] and client.procs.running == {"chrome", "firefox"}
    names = [b["name"] for b in client.get("/api/status").json()["browsers"]]
    assert names == ["chrome", "firefox"]
    page = client.get("/browsers")
    assert "Choose your browsers" not in page.text and 'value="edge" ' in page.text
    r = client.post("/browsers", data={"csrf": csrf(page.text), "browser": ["chrome"]})
    assert "Browsers updated" in r.text and client.procs.running == {"chrome"}
