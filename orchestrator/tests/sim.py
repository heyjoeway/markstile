"""Synchronous in-memory simulation of browsers and sync cycles for engine tests."""

from __future__ import annotations

import itertools
import random

from markstile.engine.cycle import BrowserInput, CyclePlan, Settings, matches_projection, plan_cycle
from markstile.engine.lift import Profile
from markstile.engine.merge import from_source, union
from markstile.engine.lift import lift
from markstile.engine.model import ROLES, Node, Tree, copy_tree
from markstile.engine.reconcile import apply_ops

CHROME_ROLES = ("toolbar", "other", "mobile")


class SimBrowser:
    _ids = itertools.count(1)

    def __init__(self, name: str, roles=None) -> None:
        self.name = name
        roles = roles or (ROLES if name.startswith("firefox") else CHROME_ROLES)
        kind = "firefox" if name.startswith("firefox") else "chrome"
        self.profile = Profile.for_browser(kind, roles)
        self.profile.browser = name
        self.tree: Tree = {r: Node.folder(f"{r}-root", id=f"{name}:{r}") for r in roles}
        self.snapshot: Tree | None = None
        self.connected = True
        self.mode = "normal"

    def new_id(self) -> str:
        return f"{self.name}:{next(self._ids)}"

    # -- editing helpers used by tests ------------------------------------
    def folders(self) -> list[Node]:
        return [n for r in self.tree.values() for n in r.walk() if n.is_folder]

    def nodes(self) -> list[Node]:
        roots = {id(r) for r in self.tree.values()}
        return [n for r in self.tree.values() for n in r.walk() if id(n) not in roots]

    def parent_of(self, node: Node) -> Node:
        for r in self.tree.values():
            for n in r.walk():
                if any(c is node for c in n.children):
                    return n
        raise KeyError(node)

    def add(self, folder: Node, node: Node, index: int | None = None) -> Node:
        if not self.profile.separators:
            if node.kind == "separator":
                return node
            for n in node.walk():
                n.children = [c for c in n.children if c.kind != "separator"]
        for n in node.walk():
            n.id = self.new_id()
        folder.children.insert(len(folder.children) if index is None else index, node)
        return node

    def apply(self, ops: list[dict]) -> None:
        # ids for created nodes come from this browser's id space
        prefix = f"{self.name}:x"
        apply_ops(self.tree, ops, id_prefix=prefix)


def random_node(rng: random.Random, depth: int = 0) -> Node:
    titles = ["a", "b", "c", "d", "News", "Dev", "x"]
    urls = ["http://1", "http://2", "http://3", "http://4"]
    r = rng.random()
    if r < 0.6 or depth > 2:
        return Node.bookmark(rng.choice(titles), rng.choice(urls))
    if r < 0.7:
        return Node.separator()
    return Node.folder(rng.choice(titles), [random_node(rng, depth + 1) for _ in range(rng.randint(0, 3))])


def random_edit(b: SimBrowser, rng: random.Random) -> None:
    # the menu fallback folder is structural in Chrome/Edge; deleting it just recreates it
    nodes = [n for n in b.nodes() if n.title != "Bookmarks Menu"]
    folders = b.folders()
    op = rng.choice(["create", "create", "delete", "move", "rename", "url"])
    if op == "create" or not nodes:
        f = rng.choice(folders)
        b.add(f, random_node(rng), rng.randint(0, len(f.children)))
        return
    n = rng.choice(nodes)
    if op == "delete":
        b.parent_of(n).children.remove(n)
    elif op == "move":
        targets = [f for f in folders if not any(x is f for x in n.walk())]
        f = rng.choice(targets)
        b.parent_of(n).children.remove(n)
        f.children.insert(rng.randint(0, len(f.children)), n)
    elif op == "rename" and n.kind != "separator":
        n.title = rng.choice(["a", "b", "renamed", "Zed"])
    elif op == "url" and n.kind == "bookmark":
        n.url = rng.choice(["http://1", "http://9", "http://8"])


class Sim:
    def __init__(self, browsers: list[SimBrowser], settings: Settings | None = None) -> None:
        self.browsers = {b.name: b for b in browsers}
        self.settings = settings or Settings()
        self.K: Tree | None = None
        self.last: CyclePlan | None = None

    def first_run(self, strategy: str = "union", source: str | None = None) -> CyclePlan:
        if strategy == "union":
            self.K = union([lift(b.tree, b.profile) for b in self.browsers.values()])
        else:
            src = self.browsers[source]
            self.K = from_source(lift(src.tree, src.profile))
        # reconcile every browser to K without merging anything back
        for b in self.browsers.values():
            b.snapshot = copy_tree(b.tree)
            b.mode = "rejected"
        return self.cycle()

    def cycle(self) -> CyclePlan:
        assert self.K is not None
        inputs = [
            BrowserInput(b.name, b.profile, copy_tree(b.tree), b.snapshot and copy_tree(b.snapshot), b.mode)
            for b in self.browsers.values()
            if b.connected
        ]
        plan = plan_cycle(self.K, inputs, self.settings)
        for i in inputs:
            b = self.browsers[i.browser]
            p = plan.browsers[i.browser]
            if p.reconcile:
                b.apply(p.ops)
                assert matches_projection(plan.K, b.tree, b.profile), (b.name, plan.K, b.tree, p.ops)
                b.snapshot = copy_tree(b.tree)
                b.mode = "normal"
        self.K = plan.K
        self.last = plan
        return plan
