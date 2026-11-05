"""Browser profiles, parsing of ``bookmarks.getTree()`` output, lifting and projection.

*Native* trees are shaped like the browser sees them (role -> root folder with
that browser's IDs).  *Lifted* trees are in canonical ``K`` coordinates: every
canonical role is present, roles the browser lacks are extracted from their
fallback folder, and unsupported node kinds are dropped.  *Projection* is the
inverse: ``K`` reshaped into what the browser should natively contain.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..browsers import is_gecko
from .model import ROLES, Node, Tree

DEFAULT_FALLBACKS = {"menu": "other/Bookmarks Menu"}


@dataclass
class Profile:
    browser: str
    native_roles: frozenset[str]
    separators: bool
    fallbacks: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_FALLBACKS))
    # Roles the browser has but hides from getTree() while empty (Chromium's
    # mobile root).  While hidden they can be neither read nor written, so they
    # lift as empty and are left out of projection.
    optional_roles: frozenset[str] = frozenset()

    @staticmethod
    def for_browser(browser: str, native_roles, fallbacks: dict[str, str] | None = None) -> "Profile":
        return Profile(
            browser=browser,
            native_roles=frozenset(native_roles),
            separators=is_gecko(browser),
            fallbacks=dict(DEFAULT_FALLBACKS if fallbacks is None else fallbacks),
            optional_roles=frozenset() if is_gecko(browser) else frozenset({"mobile"}),
        )

    def fallback_for(self, role: str) -> str | None:
        """Fallback path for a role this browser lacks, if it is usable."""
        if role in self.native_roles:
            return None
        path = self.fallbacks.get(role)
        if not path:
            return None
        base = path.split("/", 1)[0]
        if base not in self.native_roles or "/" not in path:
            return None
        return path


# -- parsing getTree() --------------------------------------------------------

CHROME_FOLDER_TYPES = {"bookmarks-bar": "toolbar", "other": "other", "mobile": "mobile"}
CHROME_LEGACY_IDS = {"1": "toolbar", "2": "other", "3": "mobile"}
FIREFOX_ROOT_IDS = {
    "toolbar_____": "toolbar",
    "unfiled_____": "other",
    "mobile______": "mobile",
    "menu________": "menu",
}


def _convert(raw: dict, browser: str) -> Node:
    if is_gecko(browser):
        kind = raw.get("type") or ("bookmark" if raw.get("url") else "folder")
    else:
        kind = "bookmark" if raw.get("url") is not None else "folder"
    if kind == "separator":
        return Node.separator(id=str(raw["id"]))
    if kind == "bookmark":
        return Node.bookmark(raw.get("title") or "", raw["url"], id=str(raw["id"]))
    return Node.folder(
        raw.get("title") or "",
        [_convert(c, browser) for c in raw.get("children") or []],
        id=str(raw["id"]),
    )


def classify_roots(raw_tree: list | dict, browser: str) -> dict[str, dict]:
    """Pick the raw root folder for each role from a getTree() result.

    Chrome may expose both account (``syncing: true``) and local-only copies of
    each permanent folder; the account copy wins.  Roles are identified by
    ``folderType`` where available and legacy IDs otherwise, never by title.
    """
    top = raw_tree[0] if isinstance(raw_tree, list) else raw_tree
    found: dict[str, dict] = {}
    for child in top.get("children") or []:
        cid = str(child.get("id"))
        if is_gecko(browser):
            role = FIREFOX_ROOT_IDS.get(cid)
        else:
            ft = child.get("folderType")
            role = CHROME_FOLDER_TYPES.get(ft) if ft else CHROME_LEGACY_IDS.get(cid)
        if role is None:
            continue
        prev = found.get(role)
        if prev is None or (child.get("syncing") is True and prev.get("syncing") is not True):
            found[role] = child
    return found


def parse_native(raw_tree: list | dict, browser: str) -> Tree:
    return {role: _convert(raw, browser) for role, raw in classify_roots(raw_tree, browser).items()}


# -- lifting / projection -------------------------------------------------------


def _drop_separators(node: Node) -> None:
    node.children = [c for c in node.children if c.kind != "separator"]
    for c in node.children:
        if c.is_folder:
            _drop_separators(c)


def _locate_fallback(native: Tree, path: str) -> tuple[Node, Node] | None:
    """Return (parent, folder) for a fallback path: intermediate segments match
    the first folder with that title, the final segment the *last* one."""
    parts = path.split("/")
    node = native.get(parts[0])
    if node is None:
        return None
    for title in parts[1:-1]:
        node = next((c for c in node.children if c.is_folder and c.title == title), None)
        if node is None:
            return None
    matches = [c for c in node.children if c.is_folder and c.title == parts[-1]]
    if not matches:
        return None
    return node, matches[-1]


def lift(native: Tree, profile: Profile) -> Tree:
    """Native tree (with ids) -> K-shaped tree (sharing no node objects)."""
    work = {r: n.copy() for r, n in native.items() if r in profile.native_roles}
    out: Tree = {}
    for role in ROLES:
        if role in work:
            continue
        path = profile.fallback_for(role)
        if path is None:
            if role in profile.optional_roles:
                out[role] = Node.folder(role)
            continue
        loc = _locate_fallback(work, path)
        if loc is None:
            out[role] = Node.folder(role)
        else:
            parent, folder = loc
            parent.children.remove(folder)
            out[role] = folder
    for role in ROLES:
        if role in work:
            out[role] = work[role]
    for role, root in out.items():
        root.title = role
        if not profile.separators:
            _drop_separators(root)
    return {r: out[r] for r in ROLES if r in out}


def _hinted_index(node: Node, current: Tree | None, path: str) -> int:
    loc = _locate_fallback(current, path) if current else None
    if loc is None:
        return len(node.children)
    parent, folder = loc
    i = next(j for j, c in enumerate(parent.children) if c is folder)
    if i == 0:
        return 0
    key = parent.children[i - 1].key()
    nth = sum(1 for c in parent.children[:i] if c.key() == key)
    seen = 0
    for j, c in enumerate(node.children):
        if c.key() == key:
            seen += 1
            if seen == nth:
                return j + 1
    return len(node.children)


def project(K: Tree, profile: Profile, current: Tree | None = None) -> Tree:
    """K -> what this browser should natively contain (no ids).

    A fallback folder goes where ``current`` (the browser's native tree, if
    given) has it -- after the sibling it currently follows -- else at the end.
    """
    out: Tree = {}
    for role in ROLES:
        if role in profile.native_roles:
            out[role] = (K.get(role) or Node.folder(role)).copy(keep_ids=False)
    for role in ROLES:
        path = profile.fallback_for(role)
        if path is None:
            continue
        parts = path.split("/")
        node = out[parts[0]]
        for title in parts[1:-1]:
            nxt = next((c for c in node.children if c.is_folder and c.title == title), None)
            if nxt is None:
                nxt = Node.folder(title)
                node.children.append(nxt)
            node = nxt
        src = K.get(role) or Node.folder(role)
        folder = Node.folder(parts[-1], [c.copy(keep_ids=False) for c in src.children])
        node.children.insert(_hinted_index(node, current, path), folder)
    if not profile.separators:
        for root in out.values():
            _drop_separators(root)
    return out
