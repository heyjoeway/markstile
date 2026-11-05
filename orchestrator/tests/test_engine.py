import json

import pytest

from sim import Sim, SimBrowser
from markstile.engine.cycle import Settings
from markstile.engine.lift import Profile, lift, parse_native, project
from markstile.engine.merge import union
from markstile.engine.model import Node, copy_tree, tree_from_json, tree_to_json, trees_equal
from markstile.engine.reconcile import apply_ops, reconcile
from markstile.engine.safety import Thresholds

B = Node.bookmark
F = Node.folder
SEP = Node.separator


def titles(node: Node) -> list[str]:
    return [("---" if c.kind == "separator" else c.title) for c in node.children]


def setup3(**kw) -> tuple[Sim, SimBrowser, SimBrowser, SimBrowser]:
    c, e, f = SimBrowser("chrome"), SimBrowser("edge"), SimBrowser("firefox")
    sim = Sim([c, e, f], **kw)
    return sim, c, e, f


# -- parsing ------------------------------------------------------------------


def test_parse_chrome_prefers_syncing_roots():
    raw = [{"id": "0", "children": [
        {"id": "1", "title": "Bookmarks bar", "folderType": "bookmarks-bar", "syncing": False, "children": [
            {"id": "10", "title": "local", "url": "http://l"}]},
        {"id": "4", "title": "Bookmarks bar", "folderType": "bookmarks-bar", "syncing": True, "children": [
            {"id": "11", "title": "acct", "url": "http://a"}]},
        {"id": "2", "title": "Other", "folderType": "other", "children": []},
        {"id": "3", "title": "Mobile", "folderType": "mobile", "children": []},
    ]}]
    t = parse_native(raw, "chrome")
    assert set(t) == {"toolbar", "other", "mobile"}
    assert t["toolbar"].id == "4" and titles(t["toolbar"]) == ["acct"]


def test_parse_chrome_legacy_ids_and_firefox():
    raw = [{"id": "0", "children": [{"id": "1", "title": "Barre", "children": []}, {"id": "2", "title": "Autres", "children": []}]}]
    assert set(parse_native(raw, "edge")) == {"toolbar", "other"}
    ff = [{"id": "root________", "children": [
        {"id": "menu________", "type": "folder", "title": "Menu", "children": [
            {"id": "s1", "type": "separator"}, {"id": "b1", "type": "bookmark", "title": "x", "url": "http://x"}]},
        {"id": "unfiled_____", "type": "folder", "title": "Other", "children": []},
        {"id": "toolbar_____", "type": "folder", "title": "Toolbar", "children": []},
        {"id": "mobile______", "type": "folder", "title": "Mobile", "children": []},
    ]}]
    t = parse_native(ff, "firefox")
    assert set(t) == {"menu", "other", "toolbar", "mobile"}
    assert [c.kind for c in t["menu"].children] == ["separator", "bookmark"]


# -- lift / project -------------------------------------------------------------


def chrome_profile():
    return Profile.for_browser("chrome", ["toolbar", "other", "mobile"])


def test_projection_menu_folder_and_separators():
    K = {"toolbar": F("toolbar", [B("a", "u"), SEP(), B("b", "v")]), "other": F("other", [B("o", "w")]),
         "mobile": F("mobile"), "menu": F("menu", [B("m", "x"), SEP()])}
    p = project(K, chrome_profile())
    assert titles(p["toolbar"]) == ["a", "b"]
    assert titles(p["other"]) == ["o", "Bookmarks Menu"]
    assert titles(p["other"].children[1]) == ["m"]
    # lifting the projection gives K back (minus separators)
    for r in p.values():
        for n in r.walk():
            n.id = str(id(n))
    lifted = lift(p, chrome_profile())
    assert titles(lifted["menu"]) == ["m"] and titles(lifted["other"]) == ["o"]


def test_lift_uses_last_menu_folder():
    nat = {"toolbar": F("t", id="1"), "mobile": F("m", id="3"),
           "other": F("o", [F("Bookmarks Menu", [B("user", "u", id="7")], id="5"),
                            F("Bookmarks Menu", [B("menu", "m", id="8")], id="6")], id="2")}
    lifted = lift(nat, chrome_profile())
    assert titles(lifted["menu"]) == ["menu"]
    assert titles(lifted["other"]) == ["Bookmarks Menu"]


def test_lift_missing_menu_folder_is_empty_role():
    nat = {"toolbar": F("t", id="1"), "other": F("o", id="2"), "mobile": F("m", id="3")}
    assert lift(nat, chrome_profile())["menu"].children == []


def test_json_roundtrip():
    t = {"toolbar": F("toolbar", [B("a", "u", id="1"), SEP("2"), F("f", [], id="3")], id="0")}
    assert trees_equal(tree_from_json(json.loads(json.dumps(tree_to_json(t, True)))), t)


# -- reconcile ------------------------------------------------------------------


def run_reconcile(target, current):
    ops = reconcile(target, current)
    cur = copy_tree(current)
    apply_ops(cur, ops)
    assert trees_equal(cur, target), (cur, target, ops)
    return ops


def test_reconcile_identical_siblings_and_minimal_moves():
    cur = {"toolbar": F("t", [B("a", "u", id="1"), B("a", "u", id="2"), B("b", "v", id="3")], id="r")}
    tgt = {"toolbar": F("t", [B("b", "v"), B("a", "u"), B("a", "u")])}
    ops = run_reconcile(tgt, cur)
    assert ops == [{"op": "move", "id": "3", "parentId": "r", "index": 0}]


def test_reconcile_rename_folder_is_update_not_recreate():
    kids = [B(str(i), f"http://{i}") for i in range(5)]
    cur = {"other": F("o", [F("Old", [B(k.title, k.url, id=f"k{i}") for i, k in enumerate(kids)], id="f")], id="r")}
    tgt = {"other": F("o", [F("New", [k.copy() for k in kids])])}
    ops = run_reconcile(tgt, cur)
    assert ops == [{"op": "update", "id": "f", "title": "New"}]


def test_reconcile_cross_folder_move_keeps_ids():
    cur = {"toolbar": F("t", [F("A", [F("Deep", [B("x", "u", id="5")], id="4")], id="2"), F("B", [], id="3")], id="r")}
    tgt = {"toolbar": F("t", [F("A", []), F("B", [F("Deep", [B("x", "u")])])])}
    ops = run_reconcile(tgt, cur)
    assert [o["op"] for o in ops] == ["move"]


def test_reconcile_wrap_into_new_folder():
    cur = {"other": F("o", [B("a", "1", id="1"), B("b", "2", id="2")], id="r")}
    tgt = {"other": F("o", [F("New", [B("a", "1"), B("b", "2")])])}
    ops = run_reconcile(tgt, cur)
    assert [o["op"] for o in ops] == ["create", "move", "move"]


def test_reconcile_bookmark_url_change_is_update():
    cur = {"other": F("o", [B("a", "1", id="1")], id="r")}
    ops = run_reconcile({"other": F("o", [B("a", "2")])}, cur)
    assert ops == [{"op": "update", "id": "1", "url": "2"}]


# -- merge / cycles -------------------------------------------------------------


def seeded():
    sim, c, e, f = setup3()
    c.add(c.tree["toolbar"], F("Work", [B("Docs", "http://docs"), B("Mail", "http://mail")]))
    c.add(c.tree["other"], B("Wiki", "http://wiki"))
    sim.first_run("source", "chrome")
    return sim, c, e, f


def test_first_run_source_reconciles_all():
    sim, c, e, f = seeded()
    for b in (c, e, f):
        assert titles(b.tree["toolbar"]) == ["Work"]
    assert titles(f.tree["other"]) == ["Wiki"]
    assert sim.cycle().changed is False


def test_first_run_union_dedupes():
    sim, c, e, f = setup3()
    c.add(c.tree["toolbar"], B("x", "http://x"))
    e.add(e.tree["toolbar"], B("x", "http://x"))
    e.add(e.tree["toolbar"], B("y", "http://y"))
    f.add(f.tree["menu"], B("m", "http://m"))
    sim.first_run("union")
    assert titles(sim.K["toolbar"]) == ["x", "y"]
    assert titles(c.tree["other"]) == ["Bookmarks Menu"]
    assert titles(c.tree["other"].children[0]) == ["m"]


def test_simple_propagation_and_idempotence():
    sim, c, e, f = seeded()
    e.add(e.tree["toolbar"].children[0], B("Cal", "http://cal"), 1)
    plan = sim.cycle()
    assert plan.browsers["edge"].ops == []
    assert titles(c.tree["toolbar"].children[0]) == ["Docs", "Cal", "Mail"]
    assert titles(f.tree["toolbar"].children[0]) == ["Docs", "Cal", "Mail"]
    assert sim.cycle().changed is False
    assert all(p.ops == [] for p in sim.last.browsers.values())


def test_rename_folder_with_new_child_in_same_cycle():
    sim, c, e, f = seeded()
    work = f.tree["toolbar"].children[0]
    work.title = "Job"
    f.add(work, B("New", "http://new"))
    sim.cycle()
    assert titles(c.tree["toolbar"]) == ["Job"]
    assert titles(c.tree["toolbar"].children[0]) == ["Docs", "Mail", "New"]
    assert c.tree["toolbar"].children[0].id == "chrome:" + c.tree["toolbar"].children[0].id.split(":")[1]


def test_edit_beats_delete():
    sim, c, e, f = seeded()
    c.tree["toolbar"].children.clear()  # chrome deletes Work (2 bookmarks of 3: hold!)
    c.mode = "approved"
    e.tree["toolbar"].children[0].children[0].title = "Docs!"
    plan = sim.cycle()
    assert any("edit beats delete" in x for x in plan.conflicts)
    assert titles(c.tree["toolbar"].children[0]) == ["Docs!", "Mail"]


def test_delete_propagates():
    sim, c, e, f = seeded()
    mail = e.tree["toolbar"].children[0].children[1]
    e.tree["toolbar"].children[0].children.remove(mail)
    sim.settings.thresholds = Thresholds(max_fraction=0.5)
    sim.cycle()
    assert titles(f.tree["toolbar"].children[0]) == ["Docs"]


def test_conflicting_edits_priority():
    sim, c, e, f = seeded()
    sim.settings.priority = ["firefox", "chrome", "edge"]
    c.tree["other"].children[0].title = "from chrome"
    f.tree["other"].children[0].title = "from firefox"
    plan = sim.cycle()
    assert titles(e.tree["other"]) == ["from firefox", "Bookmarks Menu"]
    assert len(plan.conflicts) == 1


def test_duplicate_creates_dedupe():
    sim, c, e, f = seeded()
    c.add(c.tree["other"], B("Same", "http://same"))
    e.add(e.tree["other"], B("Same", "http://same"))
    sim.cycle()
    assert titles(sim.K["other"]) == ["Wiki", "Same"]


def test_mass_delete_hold_then_reject_and_approve():
    sim, c, e, f = seeded()
    for r in e.tree.values():
        r.children.clear()
    plan = sim.cycle()
    assert plan.browsers["edge"].held is not None
    assert "empty" in plan.browsers["edge"].held.reason
    assert titles(c.tree["toolbar"]) == ["Work"]  # not spread
    # reject: edge is restored
    e.mode = "rejected"
    sim.cycle()
    assert titles(e.tree["toolbar"]) == ["Work"]
    # delete again and approve
    for r in e.tree.values():
        r.children.clear()
    sim.cycle()
    e.mode = "approved"
    sim.cycle()
    assert titles(c.tree["toolbar"]) == []


def test_offline_browser_catches_up():
    sim, c, e, f = seeded()
    f.connected = False
    c.add(c.tree["toolbar"], B("While away", "http://away"))
    sim.cycle()
    f.connected = True
    f.add(f.tree["menu"], B("ff", "http://ff"))
    sim.cycle()
    assert titles(f.tree["toolbar"]) == ["Work", "While away"]
    assert titles(c.tree["other"].children[1]) == ["ff"]


def test_inbox_mobile_to_toolbar():
    sim, c, e, f = seeded()
    sim.settings.inbox = {"mobile": "other/From phone"}
    phone_bm = c.add(c.tree["mobile"], B("Phone", "http://phone"))
    sim.cycle()
    assert c.tree["mobile"].children == []
    fp = [x for x in c.tree["other"].children if x.title == "From phone"][0]
    assert fp.children[0].id == phone_bm.id  # moved, not recreated
    assert titles(f.tree["other"]) == ["Wiki", "From phone"]
    assert sim.cycle().changed is False


def test_separators_survive_chrome_edits():
    sim, c, e, f = seeded()
    f.add(f.tree["toolbar"], SEP(), 0)
    sim.cycle()
    c.add(c.tree["toolbar"], B("first", "http://first"), 0)
    sim.cycle()
    assert [n.kind for n in f.tree["toolbar"].children] == ["bookmark", "separator", "folder"]


def test_vendor_sync_burst():
    sim, c, e, f = seeded()
    # a phone-side burst arrives in edge: several creates, a move and a rename
    work = e.tree["toolbar"].children[0]
    for i in range(10):
        e.add(work, B(f"burst{i}", f"http://b{i}"))
    e.tree["other"].children[0].title = "Wiki (renamed)"
    sim.cycle()
    assert len(f.tree["toolbar"].children[0].children) == 12
    assert titles(f.tree["other"])[0] == "Wiki (renamed)"
    assert sim.cycle().changed is False


def test_move_into_each_other_conflict():
    sim, c, e, f = setup3()
    c.add(c.tree["other"], F("X", []))
    c.add(c.tree["other"], F("Y", []))
    sim.first_run("source", "chrome")
    cx, cy = c.tree["other"].children[:2]
    c.tree["other"].children.remove(cy)
    cx.children.append(cy)
    ex, ey = e.tree["other"].children[:2]
    e.tree["other"].children.remove(ex)
    ey.children.append(ex)
    plan = sim.cycle()
    assert plan.conflicts
    assert sim.cycle().changed is False
