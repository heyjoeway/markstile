"""In-memory browser implementing the extension protocol, for tests and demos.

It mirrors the raw ``bookmarks.getTree()`` shapes of Chrome/Edge (``folderType``)
and Firefox (``type``, fixed root GUIDs) so the real parsing code is exercised.
"""

from __future__ import annotations

import itertools

from ..browsers import is_gecko
from ..engine.lift import CHROME_FOLDER_TYPES, FIREFOX_ROOT_IDS, parse_native
from ..engine.model import Node, Tree
from ..engine.reconcile import apply_ops

_CHROME_ROOTS = {v: k for k, v in CHROME_FOLDER_TYPES.items()}
_FIREFOX_ROOTS = {v: k for k, v in FIREFOX_ROOT_IDS.items()}


class FakeBrowser:
    def __init__(self, browser: str, roles=None) -> None:
        self.browser = browser
        self.ext_version = "fake"
        self.browser_version = "fake"
        self.ff = is_gecko(browser)
        roles = roles or (("menu", "toolbar", "other", "mobile") if self.ff else ("toolbar", "other", "mobile"))
        self._ids = itertools.count(100)
        self.tree: Tree = {}
        for n, r in enumerate(roles, 1):
            rid = _FIREFOX_ROOTS[r] if self.ff else str(n)
            self.tree[r] = Node.folder(r.title(), id=rid)
        self.fail_next_apply = False
        self.calls: list[str] = []

    # convenience for tests
    def add(self, role_or_folder, node: Node, index: int | None = None) -> Node:
        folder = self.tree[role_or_folder] if isinstance(role_or_folder, str) else role_or_folder
        for n in node.walk():
            n.id = f"u{next(self._ids)}"
        folder.children.insert(len(folder.children) if index is None else index, node)
        return node

    def native(self) -> Tree:
        return parse_native(self._raw(), self.browser)

    def _raw(self) -> list:
        def conv(n: Node, role: str | None = None) -> dict:
            d: dict = {"id": n.id, "title": n.title}
            if self.ff:
                d["type"] = n.kind
            elif role:
                d["folderType"] = _CHROME_ROOTS[role]
                d["syncing"] = True
            if n.kind == "bookmark":
                d["url"] = n.url
            if n.is_folder:
                d["children"] = [conv(c) for c in n.children]
            return d

        root_id = "root________" if self.ff else "0"
        # Chromium hides an empty mobile root from getTree()
        roots = [conv(n, r) for r, n in self.tree.items() if self.ff or r != "mobile" or n.children]
        return [{"id": root_id, "title": "", "children": roots}]

    async def get_tree(self):
        self.calls.append("getTree")
        return self._raw()

    async def apply_ops(self, ops: list[dict]) -> list[dict]:
        self.calls.append("applyOps")
        if self.fail_next_apply:
            self.fail_next_apply = False
            return [{"ok": False, "error": "simulated failure"} for _ in ops]
        hidden = None if self.ff or "mobile" not in self.tree or self.tree["mobile"].children else self.tree["mobile"].id
        if hidden and any(hidden in (op.get("parentId"), op.get("id")) for op in ops):
            raise RuntimeError("Can't find parent bookmark for id.")
        refs = apply_ops(self.tree, ops, id_prefix="")
        out = []
        for op in ops:
            r: dict = {"ok": True}
            if op["op"] == "create":
                r["id"] = refs[op["ref"]]
            out.append(r)
        return out
