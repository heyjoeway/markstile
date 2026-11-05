import asyncio
import json

import pytest
import websockets

from markstile.adapters.base import Registry
from markstile.adapters.fake import FakeBrowser
from markstile.adapters.ws import ExtensionServer
from markstile.engine.model import Node
from markstile.scheduler import Scheduler
from markstile.store import Store
from markstile.sync import Syncer

B = Node.bookmark
F = Node.folder


def titles(n):
    return [c.title for c in n.children]


async def make(n=3):
    store = Store(":memory:")
    reg = Registry()
    fakes = [FakeBrowser("chrome"), FakeBrowser("edge"), FakeBrowser("firefox")][:n]
    for f in fakes:
        await reg.add(f)
    s = store.settings()
    s.browsers = [f.browser for f in fakes]
    store.save_settings(s)
    return store, reg, Syncer(store, reg), fakes


async def test_sync_disabled_until_first_run():
    store, reg, syncer, (c, e, f) = await make()
    c.add("toolbar", B("a", "http://a"))
    r = await syncer.run_cycle()
    assert "skipped" in r
    assert e.tree["toolbar"].children == []
    assert store.browser("chrome").bookmarks == 1


async def test_first_run_source_and_propagation():
    store, reg, syncer, (c, e, f) = await make()
    c.add("toolbar", F("Work", [B("Docs", "http://docs")]))
    e.add("other", B("edge-only", "http://e"))
    preview = await syncer.first_run_preview()
    assert preview["counts"] == {"chrome": 1, "edge": 1, "firefox": 0}
    assert preview["removals"]["chrome"]["edge"] == 1
    await syncer.first_run("source", "chrome")
    assert titles(e.tree["toolbar"]) == ["Work"]
    assert titles(e.tree["other"]) == ["Bookmarks Menu"]
    assert titles(f.tree["toolbar"]) == ["Work"]
    # an edit in firefox reaches chrome
    f.add(f.tree["toolbar"].children[0], B("Cal", "http://cal"))
    r = await syncer.run_cycle("test")
    assert r["changed"] and r["ops"]["firefox"] == 0
    assert titles(c.tree["toolbar"].children[0]) == ["Docs", "Cal"]
    assert len(store.versions()) == 2
    r = await syncer.run_cycle("again")
    assert not r["changed"] and set(r["ops"].values()) == {0}


async def test_hold_and_approve_flow():
    store, reg, syncer, (c, e, f) = await make()
    for i in range(10):
        c.add("other", B(f"b{i}", f"http://{i}"))
    await syncer.first_run("union")
    e.tree["other"].children = [x for x in e.tree["other"].children if x.title == "Bookmarks Menu"]
    r = await syncer.run_cycle()
    assert "edge" in r["held"]
    assert store.browser("edge").hold["deleted"] == 10
    assert len(c.tree["other"].children) == 11
    await syncer.resolve_hold("edge", approve=True)
    assert titles(c.tree["other"]) == ["Bookmarks Menu"]
    assert store.browser("edge").hold is None


async def test_restore_version():
    store, reg, syncer, (c, e, f) = await make()
    c.add("toolbar", B("keep", "http://k"))
    await syncer.first_run("union")
    v1 = store.versions()[0]["id"]
    c.add("toolbar", B("later", "http://l"))
    await syncer.run_cycle()
    assert titles(f.tree["toolbar"]) == ["keep", "later"]
    await syncer.restore(v1)
    for b in (c, e, f):
        assert titles(b.tree["toolbar"]) == ["keep"]


async def test_disconnected_browser_catches_up_and_failed_apply_rejoins():
    store, reg, syncer, (c, e, f) = await make()
    await syncer.first_run("union")
    await reg.remove(f)
    c.add("toolbar", B("x", "http://x"))
    await syncer.run_cycle()
    await reg.add(f)
    await syncer.run_cycle()
    assert titles(f.tree["toolbar"]) == ["x"]
    # an apply that fails entirely is retried once and then reported
    c.add("toolbar", B("y", "http://y"))
    e.fail_next_apply = True
    await syncer.run_cycle()
    assert titles(e.tree["toolbar"]) == ["x", "y"]  # retry succeeded


async def test_inbox_setting():
    store, reg, syncer, (c, e, f) = await make()
    await syncer.first_run("union")
    s = store.settings()
    s.root_modes["mobile"] = "toolbar"
    store.save_settings(s)
    e.add("mobile", B("phone", "http://p"))
    await syncer.run_cycle()
    assert titles(c.tree["toolbar"]) == ["phone"]
    assert e.tree["mobile"].children == []


async def test_scheduler_debounce_and_startup():
    store, reg, syncer, fakes = await make()
    calls = []
    orig = syncer.run_cycle

    async def spy(reason="x"):
        calls.append(reason)
        return await orig(reason)

    syncer.run_cycle = spy
    sch = Scheduler(syncer, quiet=0.05, max_wait=0.2, startup_timeout=5)
    reg.on_event = sch.on_event
    sch.start()
    await asyncio.sleep(0.05)
    assert calls == ["startup"]
    for _ in range(5):
        sch.on_event("chrome", {})
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.15)
    assert calls == ["startup", "bookmark events"]
    sch.request("manual")
    await asyncio.sleep(0.05)
    assert calls[-1] == "manual"
    await sch.stop()


async def test_websocket_bridge_roundtrip(tmp_path):
    """A Python client speaking the extension protocol, backed by a FakeBrowser."""
    (tmp_path / "update.xml").write_text("<gupdate/>")
    reg = Registry()
    events = []
    reg.on_event = lambda b, ev: events.append((b, ev["kind"]))
    srv = ExtensionServer(reg, secret="s3cret", port=0, ext_dir=tmp_path)
    await srv.start()
    url = f"ws://127.0.0.1:{srv.port}"
    fake = FakeBrowser("edge")

    async def client(secret):
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"type": "hello", "browser": "edge", "extVersion": "1", "browserVersion": "x", "secret": secret}))
            await ws.send(json.dumps({"type": "event", "kind": "onCreated"}))
            async for raw in ws:
                msg = json.loads(raw)
                if msg["type"] == "request":
                    if msg["method"] == "getTree":
                        res = await fake.get_tree()
                    else:
                        res = await fake.apply_ops(msg["params"]["ops"])
                    await ws.send(json.dumps({"type": "response", "id": msg["id"], "result": res}))

    with pytest.raises(websockets.ConnectionClosed):
        await asyncio.wait_for(client("wrong"), 2)
    unconfigured = []
    srv.on_unconfigured = unconfigured.append
    with pytest.raises(websockets.ConnectionClosed):
        await asyncio.wait_for(client(""), 2)
    assert unconfigured == ["edge"] and "edge" not in reg.sessions
    task = asyncio.create_task(client("s3cret"))
    for _ in range(100):
        if "edge" in reg.sessions:
            break
        await asyncio.sleep(0.01)
    sess = reg.sessions["edge"]
    raw = await sess.get_tree()
    assert raw[0]["children"][0]["folderType"] == "bookmarks-bar"
    res = await sess.apply_ops([{"op": "create", "ref": "$1", "parentId": "1", "index": 0, "type": "bookmark", "title": "t", "url": "http://t"}])
    assert res[0]["ok"] and titles(fake.tree["toolbar"]) == ["t"]
    assert ("edge", "onCreated") in events
    # update server
    async with websockets.connect(url) as _:
        pass
    import urllib.request
    body = await asyncio.to_thread(lambda: urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/ext/update.xml").read())
    assert body == b"<gupdate/>"
    task.cancel()
    await srv.stop()


async def test_changing_menu_folder_moves_it_without_merging():
    store, reg, syncer, (c, e, f) = await make()
    f.add("menu", B("m", "http://m"))
    await syncer.first_run("union")
    assert titles(c.tree["other"]) == ["Bookmarks Menu"]
    s = store.settings()
    s.menu_folder = "other/Firefox Menu"
    store.save_settings(s)
    for b in ("chrome", "edge"):
        store.update_browser(b, mode="rejected")
    r = await syncer.run_cycle()
    assert not r["changed"]
    assert titles(c.tree["other"]) == ["Firefox Menu"]
    assert titles(c.tree["other"].children[0]) == ["m"]


async def test_hidden_empty_chromium_mobile_root():
    store, reg, syncer, (c, e, f) = await make()
    s = store.settings()
    s.root_modes["mobile"] = "mirror"
    store.save_settings(s)
    f.add("mobile", B("ff-mobile", "http://fm"))
    await syncer.first_run("union")
    # chrome/edge mobile roots are empty, hence hidden: nothing is written there
    assert c.tree["mobile"].children == [] and store.browser("chrome").last_error is None
    assert not (await syncer.run_cycle())["changed"]
    # a phone saves into Chrome's mobile root: it appears and the item reaches Firefox
    c.add("mobile", B("phone", "http://p"))
    r = await syncer.run_cycle()
    assert r["changed"] and titles(f.tree["mobile"]) == ["phone", "ff-mobile"]
    assert titles(c.tree["mobile"]) == ["phone", "ff-mobile"]  # visible now: mirrored
    assert e.tree["mobile"].children == []  # still hidden in Edge
    # emptying the role everywhere hides Chrome's root again without errors
    f.tree["mobile"].children.clear()
    r = await syncer.run_cycle()
    assert "firefox" in r["held"]  # its whole tree emptied
    await syncer.resolve_hold("firefox", approve=True)
    assert c.tree["mobile"].children == []
    assert {b: store.browser(b).last_error for b in ("chrome", "edge", "firefox")} == dict.fromkeys(("chrome", "edge", "firefox"))
    assert not (await syncer.run_cycle())["changed"]


async def test_default_mobile_inbox():
    store, reg, syncer, (c, e, f) = await make()
    f.add("mobile", B("ff-mobile", "http://fm"))
    c.add("toolbar", B("t", "http://t"))
    await syncer.first_run("union")
    def inbox(b):
        return next(x for x in b.tree["other"].children if x.title == "From phone")

    for b in (c, e, f):
        assert b.tree["mobile"].children == []
        assert titles(inbox(b)) == ["ff-mobile"]
    c.add("mobile", B("phone", "http://p"))
    r = await syncer.run_cycle()
    assert r["changed"] and c.tree["mobile"].children == []
    assert titles(inbox(e)) == ["ff-mobile", "phone"]


async def test_restart_right_after_write_rejoins_instead_of_deleting():
    store, reg, syncer, (c, e, f) = await make()
    for b in (c, e, f):
        b.boot_id = "boot-1"
    reg.on_connect = syncer.on_connect
    for b in (c, e, f):
        syncer.on_connect(b.browser, True)
    f.add("toolbar", B("new", "http://n"))
    await syncer.first_run("union")
    assert titles(c.tree["toolbar"]) == ["new"]
    # chrome crashes right after our write: the bookmark never reached disk
    await reg.remove(c)
    c.tree["toolbar"].children.clear()
    c.boot_id = "boot-2"
    await reg.add(c)
    await syncer.run_cycle()
    assert titles(f.tree["toolbar"]) == ["new"]  # not deleted everywhere
    assert titles(c.tree["toolbar"]) == ["new"]  # restored in chrome


async def test_restart_long_after_write_keeps_deletions():
    store, reg, syncer, (c, e, f) = await make()
    for b in (c, e, f):
        b.boot_id = "boot-1"
    reg.on_connect = syncer.on_connect
    for b in (c, e, f):
        syncer.on_connect(b.browser, True)
    for i in range(10):
        f.add("toolbar", B(f"b{i}", f"http://{i}"))
    await syncer.first_run("union")
    store.set("lastwrite.chrome", store.get("lastseen.chrome") - 3600)
    c.tree["toolbar"].children.pop()  # a genuine deletion, then a restart
    await reg.remove(c)
    c.boot_id = "boot-2"
    await reg.add(c)
    await syncer.run_cycle()
    assert len(f.tree["toolbar"].children) == 9


async def test_unchosen_browser_is_ignored():
    store, reg, syncer, (c, e, f) = await make()
    await syncer.set_browsers(["chrome", "firefox"])
    e.add("toolbar", B("edge-only", "http://e"))
    await syncer.first_run("union")
    assert titles(c.tree["toolbar"]) == [] and titles(e.tree["toolbar"]) == ["edge-only"]
    c.add("toolbar", B("c", "http://c"))
    await syncer.run_cycle()
    assert titles(f.tree["toolbar"]) == ["c"] and titles(e.tree["toolbar"]) == ["edge-only"]


async def test_browser_added_after_first_run_waits_then_joins_by_union():
    store, reg, syncer, (c, e, f) = await make()
    await syncer.set_browsers(["chrome", "firefox"])
    c.add("toolbar", B("c", "http://c"))
    await syncer.first_run("union")
    added, removed = await syncer.set_browsers(["chrome", "edge", "firefox"])
    assert added == ["edge"] and not removed
    assert store.settings().paused == ["edge"] and syncer.enabled() == ["chrome", "firefox"]
    e.add("toolbar", B("e", "http://e"))  # e.g. arriving from vendor sync after sign-in
    await syncer.run_cycle()
    assert titles(e.tree["toolbar"]) == ["e"]  # untouched while paused
    with pytest.raises(ValueError):
        await syncer.join("chrome")
    await syncer.join("edge")
    assert store.settings().paused == []
    for b in (c, e, f):
        assert sorted(titles(b.tree["toolbar"])) == ["c", "e"]


async def test_removed_browser_is_left_alone_and_can_rejoin():
    store, reg, syncer, (c, e, f) = await make()
    c.add("toolbar", B("c", "http://c"))
    await syncer.first_run("union")
    await syncer.set_browsers(["chrome", "edge"])
    assert store.snapshot("firefox") is None
    c.tree["toolbar"].children[0].title = "c2"
    await syncer.run_cycle()
    assert titles(e.tree["toolbar"]) == ["c2"] and titles(f.tree["toolbar"]) == ["c"]
    await syncer.set_browsers(["chrome", "edge", "firefox"])
    await syncer.join("firefox")  # its stale "c" is merged back, nothing deleted
    assert sorted(titles(c.tree["toolbar"])) == ["c", "c2"]


def test_existing_install_keeps_its_browsers():
    store = Store(":memory:")
    assert store.settings().browsers is None
    store.update_browser("firefox", bookmarks=1)
    store.update_browser("chrome", bookmarks=1)
    assert store.settings().browsers == ["chrome", "firefox"]


async def test_browser_processes(tmp_path):
    from markstile.browsers import BROWSERS
    from markstile.procs import BrowserProcesses

    log = tmp_path / "ctl.log"
    ctl = ["sh", "-c", f'echo "$*" >> {log}; [ "$1" = stop ] && {{ echo "$2: ERROR (not running)"; exit 7; }}; exit 0', "ctl"]
    procs = BrowserProcesses(ctl, tmp_path / "browsers")
    assert await procs.apply(["firefox", "chrome", "bogus"]) == []
    stops = [f"stop {b}" for b in BROWSERS if b not in ("chrome", "firefox")]
    assert sorted(log.read_text().splitlines()) == sorted(["start chrome", "start firefox", *stops])
    assert procs.running == {"chrome", "firefox"}
    assert (tmp_path / "browsers").read_text() == "chrome\tGoogle Chrome\nfirefox\tFirefox Developer Edition\n"
    bad = BrowserProcesses(["sh", "-c", 'echo "boom"; exit 1', "ctl"])
    errs = await bad.apply(["chrome"])
    assert len(errs) == len(BROWSERS) and "boom" in errs[0] and bad.running == set()


async def test_scheduled_restart_waits_for_an_idle_console(tmp_path, monkeypatch):
    import time as _time

    from markstile import restarts
    from markstile.procs import BrowserProcesses

    log = tmp_path / "ctl.log"
    store = Store(":memory:")
    s = store.settings()
    s.browsers = ["firefox", "chrome"]
    store.save_settings(s)
    procs = BrowserProcesses(["sh", "-c", f'echo "$*" >> {log}', "ctl"])
    console = restarts.ConsoleActivity()
    r = restarts.BrowserRestarter(store, procs, Syncer(store, Registry()), console)
    now = [_time.time()]
    mono = [1000.0]
    monkeypatch.setattr(restarts, "wall_clock", lambda: now[0])
    monkeypatch.setattr(restarts, "monotonic", lambda: mono[0])
    r.last_restart = now[0]

    assert not await r.check()  # not due yet
    now[0] += 24 * 3600
    console.opened()
    mono[0] += 60
    console.client_message(bytes([3, 1, 0, 0]))  # a screen update request isn't use
    mono[0] += 200
    assert not await r.check()  # someone was at the console 4 min ago
    console.client_message(bytes([5, 0, 0, 10, 0, 10]))  # pointer event
    mono[0] += restarts.CONSOLE_IDLE - 1
    assert not await r.check()
    mono[0] += 1
    assert await r.check()
    assert log.read_text().splitlines() == ["restart chrome", "restart firefox"]
    assert r.last_restart == now[0] and not await r.check()
    assert "scheduled restart: Google Chrome, Firefox Developer Edition" in store.logs()[0]["message"]

    s.restart_hours = 0
    store.save_settings(s)
    now[0] += 1000 * 3600
    assert r.due_at() is None and not await r.check()


async def test_browser_without_its_extension_settings_is_restarted_once(tmp_path, monkeypatch):
    from markstile import restarts
    from markstile.procs import BrowserProcesses

    log = tmp_path / "ctl.log"
    store = Store(":memory:")
    s = store.settings()
    s.browsers = ["chrome"]
    store.save_settings(s)
    r = restarts.BrowserRestarter(store, BrowserProcesses(["sh", "-c", f'echo "$*" >> {log}', "ctl"]),
                                  Syncer(store, Registry()), restarts.ConsoleActivity())
    mono = [1000.0]
    monkeypatch.setattr(restarts, "monotonic", lambda: mono[0])

    def attempts(browser, seconds):
        for _ in range(seconds):
            r.extension_unconfigured(browser)
            mono[0] += 1

    attempts("chrome", restarts.UNCONFIGURED_FOR)
    await asyncio.sleep(0.2)
    assert not log.exists()
    attempts("chrome", 1)
    attempts("edge", 30)  # not one of the chosen browsers
    for _ in range(50):
        if log.exists():
            break
        await asyncio.sleep(0.05)
    assert log.read_text().splitlines() == ["restart chrome"]
    attempts("chrome", 120)  # still failing: no second restart within ten minutes
    await asyncio.sleep(0.2)
    assert log.read_text().splitlines() == ["restart chrome"]
    assert "hasn't received its settings" in store.logs()[0]["message"]


async def test_a_browser_that_did_not_stop_keeps_its_profile(tmp_path):
    from markstile.procs import BrowserProcesses

    procs = BrowserProcesses(["true"], profiles=tmp_path)
    (tmp_path / "brave" / "Default").mkdir(parents=True)
    procs.running = {"brave"}
    erased, errors = await procs.erase_profiles(["brave", "bogus"])
    assert erased == [] and "didn't stop" in errors[0] and (tmp_path / "brave" / "Default").exists()


def test_vivaldi_new_tabs_are_blank(tmp_path):
    import json

    from markstile import prefs

    assert prefs.apply("vivaldi", tmp_path)  # a new profile
    f = tmp_path / "Default" / "Preferences"
    assert json.loads(f.read_text())["vivaldi"]["tabs"]["new_page"]["link"] == 2
    f.write_text(json.dumps({"vivaldi": {"tabs": {"new_page": {"link": 0, "custom_url": "x"}}, "other": 1}}))
    prefs.apply("vivaldi", tmp_path)
    assert json.loads(f.read_text())["vivaldi"] == {"tabs": {"new_page": {"link": 2, "custom_url": "x"}}, "other": 1}
    f.write_text("{broken")
    assert not prefs.apply("vivaldi", tmp_path) and f.read_text() == "{broken"
    assert not prefs.apply("chrome", tmp_path)


async def vivaldi_with_defaults(store, reg):
    v = FakeBrowser("vivaldi")
    v.add("toolbar", F("Home", [B("Vivaldi Browser", "https://vivaldi.com/")]))
    v.add("toolbar", F("Shopping", [B("eBay", "https://vivaldi.com/bk/ebay-en-us")]))
    v.add("other", F("Empty", []))
    await reg.add(v)
    s = store.settings()
    s.browsers = [*s.browsers, "vivaldi"]
    store.save_settings(s)
    return v


async def test_new_vivaldi_profile_is_emptied_before_it_syncs():
    store, reg, syncer, (c, e, f) = await make()
    c.add("toolbar", B("c", "http://c"))
    v = await vivaldi_with_defaults(store, reg)
    await syncer.first_run("union")  # first contact happens inside the cycle, under the lock
    assert titles(v.tree["toolbar"]) == ["c"] and titles(c.tree["toolbar"]) == ["c"]
    assert "Empty" not in titles(v.tree["other"])
    assert any("default bookmarks" in l["message"] for l in store.logs())


async def test_vivaldi_defaults_arriving_later_are_kept():
    store, reg, syncer, (c, e, f) = await make()
    v = FakeBrowser("vivaldi")
    v.add("toolbar", B("mine", "https://example.org/"))
    await reg.add(v)
    await syncer.first_contact("vivaldi")  # not a fresh profile: nothing removed, checked once
    assert titles(v.tree["toolbar"]) == ["mine"]
    v.tree["toolbar"].children.clear()
    v.add("toolbar", B("eBay", "https://vivaldi.com/bk/ebay-en-us"))  # e.g. from Vivaldi Sync
    await syncer.first_contact("vivaldi")
    assert titles(v.tree["toolbar"]) == ["eBay"]


async def test_new_firefox_profile_loses_its_mozilla_folder():
    store, reg, syncer, (c, e, f) = await make()
    f.add("menu", F("Mozilla Firefox", [B("About Us", "https://www.mozilla.org/about/"),
                                        B("Get Help", "https://support.mozilla.org/products/firefox")]))
    await syncer.first_contact("firefox")
    assert titles(f.tree["menu"]) == []


async def test_first_contact_ignores_browsers_without_defaults():
    store, reg, syncer, (c, e, f) = await make()
    c.add("toolbar", B("Google", "https://www.google.com/"))
    await syncer.first_contact("chrome")
    assert titles(c.tree["toolbar"]) == ["Google"] and store.get("firstcontact.chrome") is None
