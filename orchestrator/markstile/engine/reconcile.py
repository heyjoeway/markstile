"""Content tree diff: compute extension ops that turn a browser's current
native tree into a target tree (the projection of ``K``).

Ops reference the browser's own IDs.  Created nodes get placeholder refs
(``"$1"``, ``"$2"``, …) that later ops may use as ``id``/``parentId``; the
extension substitutes the real IDs as it applies ops in order.  ``index`` is
always the node's final index in the destination folder after the op, which
the extension adapts to each browser's ``move()`` semantics.
"""

from __future__ import annotations

from .match import match_trees
from .model import Node, Tree


def _lis(seq: list[int]) -> set[int]:
    """Indices (into seq) of one longest strictly increasing subsequence."""
    import bisect

    tails: list[int] = []
    tails_idx: list[int] = []
    prev = [-1] * len(seq)
    for i, v in enumerate(seq):
        j = bisect.bisect_left(tails, v)
        if j == len(tails):
            tails.append(v)
            tails_idx.append(i)
        else:
            tails[j] = v
            tails_idx[j] = i
        prev[i] = tails_idx[j - 1] if j > 0 else -1
    out: set[int] = set()
    k = tails_idx[-1] if tails_idx else -1
    while k != -1:
        out.add(k)
        k = prev[k]
    return out


def reconcile(target: Tree, current: Tree) -> list[dict]:
    """Ops turning ``current`` (native, with ids) into ``target`` (no ids).

    Roles absent from either side are ignored.  ``current`` is not modified.
    """
    C = {r: n.copy() for r, n in current.items()}
    T = {r: n for r, n in target.items() if r in C}
    m = match_trees(T, C)
    parent: dict[int, Node] = {}
    for root in C.values():
        for n in root.walk():
            for ch in n.children:
                parent[id(ch)] = n
    ops: list[dict] = []
    counter = 0

    def place(node: Node, folder: Node, after: Node | None) -> int:
        old = parent.get(id(node))
        if old is not None:
            old.children.remove(node)
        idx = 0 if after is None else folder.children.index(after) + 1
        folder.children.insert(idx, node)
        parent[id(node)] = folder
        return idx

    def process(tf: Node, cf: Node) -> None:
        nonlocal counter
        # stable = matched children already in this folder, in LIS order
        here = [(i, m.get(t)) for i, t in enumerate(tf.children)]
        cur_pos = {id(c): j for j, c in enumerate(cf.children)}
        cand = [(i, cur_pos[id(c)]) for i, c in here if c is not None and id(c) in cur_pos]
        stable_i = {cand[k][0] for k in _lis([p for _, p in cand])}
        prev: Node | None = None
        for i, t in enumerate(tf.children):
            c = m.get(t)
            if c is None:
                counter += 1
                ref = f"${counter}"
                c = Node(t.kind, t.title, t.url, [], ref)
                idx = place(c, cf, prev)
                op = {"op": "create", "ref": ref, "parentId": cf.id, "index": idx, "type": t.kind}
                if t.kind != "separator":
                    op["title"] = t.title
                if t.kind == "bookmark":
                    op["url"] = t.url
                ops.append(op)
                m.pair(t, c)
            else:
                if i not in stable_i:
                    old = parent.get(id(c))
                    expected = 0 if prev is None else cf.children.index(prev) + 1
                    in_place = old is cf and cf.children.index(c) == expected
                    if not in_place:
                        idx = place(c, cf, prev)
                        ops.append({"op": "move", "id": c.id, "parentId": cf.id, "index": idx})
                if t.kind != "separator" and (c.title != t.title or c.url != t.url):
                    upd: dict = {"op": "update", "id": c.id}
                    if c.title != t.title:
                        upd["title"] = t.title
                        c.title = t.title
                    if t.kind == "bookmark" and c.url != t.url:
                        upd["url"] = t.url
                        c.url = t.url
                    ops.append(upd)
            prev = c
        for t in tf.children:
            if t.is_folder:
                process(t, m.get(t))

    for r in T:
        process(T[r], C[r])

    def sweep(folder: Node) -> None:
        for ch in list(folder.children):
            if not m.c_matched(ch):
                ops.append({"op": "removeTree" if ch.is_folder else "remove", "id": ch.id})
                folder.children.remove(ch)
            elif ch.is_folder:
                sweep(ch)

    for r in T:
        sweep(C[r])
    return ops


def apply_ops(tree: Tree, ops: list[dict], id_prefix: str = "n") -> dict[str, str]:
    """Reference implementation of the extension's op semantics on an
    in-memory native tree.  Returns the ref -> id mapping.  Used by the fake
    adapter and tests; raises ``KeyError``/``ValueError`` on invalid ops."""
    index: dict[str, Node] = {}
    parent: dict[str, Node] = {}
    for root in tree.values():
        for n in root.walk():
            index[n.id] = n
            for ch in n.children:
                parent[ch.id] = n
    refs: dict[str, str] = {}
    counter = [max([int(i[len(id_prefix):]) for i in index if i and i.startswith(id_prefix) and i[len(id_prefix):].isdigit()] + [0])]

    def res(x: str) -> str:
        return refs.get(x, x)

    for op in ops:
        kind = op["op"]
        if kind == "create":
            p = index[res(op["parentId"])]
            counter[0] += 1
            nid = f"{id_prefix}{counter[0]}"
            n = Node(op.get("type", "bookmark" if op.get("url") else "folder"), op.get("title", ""), op.get("url"), [], nid)
            if n.kind == "folder":
                n.url = None
            p.children.insert(min(op["index"], len(p.children)), n)
            index[nid] = n
            parent[nid] = p
            refs[op["ref"]] = nid
        elif kind == "move":
            n = index[res(op["id"])]
            old = parent[n.id]
            old.children.remove(n)
            p = index[res(op["parentId"])]
            p.children.insert(min(op["index"], len(p.children)), n)
            parent[n.id] = p
        elif kind == "update":
            n = index[res(op["id"])]
            if "title" in op:
                n.title = op["title"]
            if "url" in op:
                n.url = op["url"]
        elif kind in ("remove", "removeTree"):
            n = index[res(op["id"])]
            if kind == "remove" and n.children:
                raise ValueError("remove on non-empty folder")
            parent[n.id].children.remove(n)
            for x in n.walk():
                index.pop(x.id, None)
        else:
            raise ValueError(f"unknown op {kind}")
    return refs
