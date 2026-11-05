"""Normalized bookmark tree model.

A tree is a mapping ``role -> Node`` where each root Node is a folder.  Nodes
taken from a browser carry that browser's local ``id``; nodes in the canonical
tree ``K`` have ``id=None``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Literal

Kind = Literal["folder", "bookmark", "separator"]
ROLES: tuple[str, ...] = ("toolbar", "other", "mobile", "menu")

Key = tuple


@dataclass(eq=False)
class Node:
    kind: Kind
    title: str = ""
    url: str | None = None
    children: list["Node"] = field(default_factory=list)
    id: str | None = None

    # -- constructors -----------------------------------------------------
    @staticmethod
    def folder(title: str = "", children: list["Node"] | None = None, id: str | None = None) -> "Node":
        return Node("folder", title, None, list(children or []), id)

    @staticmethod
    def bookmark(title: str, url: str, id: str | None = None) -> "Node":
        return Node("bookmark", title, url, [], id)

    @staticmethod
    def separator(id: str | None = None) -> "Node":
        return Node("separator", "", None, [], id)

    # -- helpers ----------------------------------------------------------
    @property
    def is_folder(self) -> bool:
        return self.kind == "folder"

    def key(self) -> Key:
        if self.kind == "folder":
            return ("f", self.title)
        if self.kind == "bookmark":
            return ("b", self.title, self.url)
        return ("s",)

    def walk(self) -> Iterator["Node"]:
        """Pre-order walk including self."""
        yield self
        for c in self.children:
            yield from c.walk()

    def copy(self, keep_ids: bool = True) -> "Node":
        return Node(
            self.kind,
            self.title,
            self.url,
            [c.copy(keep_ids) for c in self.children],
            self.id if keep_ids else None,
        )

    def content_equal(self, other: "Node") -> bool:
        if self.key() != other.key() or len(self.children) != len(other.children):
            return False
        return all(a.content_equal(b) for a, b in zip(self.children, other.children))

    def count_bookmarks(self) -> int:
        return sum(1 for n in self.walk() if n.kind == "bookmark")

    def to_json(self, with_ids: bool = False) -> dict:
        d: dict = {"type": self.kind}
        if self.kind != "separator":
            d["title"] = self.title
        if self.kind == "bookmark":
            d["url"] = self.url
        if self.kind == "folder":
            d["children"] = [c.to_json(with_ids) for c in self.children]
        if with_ids and self.id is not None:
            d["id"] = self.id
        return d

    @staticmethod
    def from_json(d: dict) -> "Node":
        kind = d["type"]
        return Node(
            kind,
            d.get("title", ""),
            d.get("url"),
            [Node.from_json(c) for c in d.get("children", [])] if kind == "folder" else [],
            d.get("id"),
        )

    def __repr__(self) -> str:  # compact, for test failures
        if self.kind == "separator":
            return "---"
        if self.kind == "bookmark":
            return f"<{self.title}|{self.url}>"
        return f"{self.title or '[]'}{self.children!r}"


Tree = dict[str, Node]


def tree_to_json(tree: Tree, with_ids: bool = False) -> dict:
    return {role: root.to_json(with_ids) for role, root in tree.items()}


def tree_from_json(d: dict) -> Tree:
    return {role: Node.from_json(v) for role, v in d.items()}


def copy_tree(tree: Tree, keep_ids: bool = True) -> Tree:
    return {role: root.copy(keep_ids) for role, root in tree.items()}


def trees_equal(a: Tree, b: Tree) -> bool:
    """Content equality of two trees, ignoring ids and root titles."""
    if set(a) != set(b):
        return False
    return all(
        len(a[r].children) == len(b[r].children)
        and all(x.content_equal(y) for x, y in zip(a[r].children, b[r].children))
        for r in a
    )


def count_bookmarks(tree: Tree) -> int:
    return sum(root.count_bookmarks() for root in tree.values())


def empty_tree() -> Tree:
    return {role: Node.folder(role) for role in ROLES}


def parent_map(tree: Tree) -> dict[int, Node]:
    """Maps id(node) -> parent node for every non-root node."""
    out: dict[int, Node] = {}
    for root in tree.values():
        for n in root.walk():
            for c in n.children:
                out[id(c)] = n
    return out


def find_path(tree: Tree, path: str, create: bool = False) -> Node | None:
    """Resolve a slash path such as ``other/Bookmarks Menu`` to a folder.

    The first segment is a role.  Each further segment matches the first child
    folder with that title.  With ``create=True`` missing folders are appended.
    """
    parts = [p for p in path.split("/") if p]
    if not parts or parts[0] not in tree:
        return None
    node = tree[parts[0]]
    for title in parts[1:]:
        nxt = next((c for c in node.children if c.is_folder and c.title == title), None)
        if nxt is None:
            if not create:
                return None
            nxt = Node.folder(title)
            node.children.append(nxt)
        node = nxt
    return node
