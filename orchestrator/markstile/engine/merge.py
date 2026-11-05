"""ID diff (snapshot -> current, per browser) and merge into ``K``.

Each browser's change set is derived from its lifted snapshot ``S`` and lifted
current tree ``C`` using the browser's own IDs.  Snapshot IDs are mapped onto
canonical nodes by content matching (``S`` vs ``K``), so operations apply to
``K`` by content rather than through any stored ID mapping table.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .match import match_trees
from .model import ROLES, Node, Tree, copy_tree, find_path
from .reconcile import _lis


@dataclass
class BrowserChange:
    browser: str
    S: Tree  # lifted snapshot, with ids
    C: Tree  # lifted current tree, with ids


@dataclass
class Stats:
    created: int = 0
    deleted: int = 0
    updated: int = 0
    moved: int = 0

    def total(self) -> int:
        return self.created + self.deleted + self.updated + self.moved

    def as_dict(self) -> dict:
        return {"created": self.created, "deleted": self.deleted, "updated": self.updated, "moved": self.moved}


@dataclass
class MergeResult:
    K: Tree
    stats: dict[str, Stats] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


def _index(tree: Tree) -> tuple[dict[str, Node], dict[str, Node], dict[str, str]]:
    """id -> node, id -> parent node, id -> role (roots included in nodes)."""
    nodes: dict[str, Node] = {}
    parents: dict[str, Node] = {}
    roles: dict[str, str] = {}
    for role, root in tree.items():
        for n in root.walk():
            if n.id is not None:
                nodes[n.id] = n
                roles[n.id] = role
            for ch in n.children:
                if ch.id is not None:
                    parents[ch.id] = n
    return nodes, parents, roles


def diff_stats(S: Tree, C: Tree) -> Stats:
    """Counts of changes between two lifted trees of one browser."""
    sn, sp, _ = _index(S)
    cn, cp, _ = _index(C)
    st = Stats()
    root_ids = {r.id for r in list(S.values()) + list(C.values())}
    for i, n in cn.items():
        if i in root_ids:
            continue
        if i not in sn:
            st.created += 1
            continue
        o = sn[i]
        if (o.title, o.url) != (n.title, n.url) and n.kind != "separator":
            st.updated += 1
        if sp.get(i) is not None and cp.get(i) is not None and sp[i].id != cp[i].id:
            st.moved += 1
    for i, f in cn.items():  # reorders within the same folder
        if i not in sn or not f.is_folder:
            continue
        s_pos = {x.id: j for j, x in enumerate(sn[i].children)}
        common = [s_pos[x.id] for x in f.children if x.id in s_pos]
        st.moved += len(common) - len(_lis(common))
    st.deleted = sum(1 for i in sn if i not in cn and i not in root_ids)
    return st


def _contains(anc: Node, node: Node) -> bool:
    return any(n is node for n in anc.walk())


def merge(K: Tree, changes: list[BrowserChange]) -> MergeResult:
    """Apply every browser's changes to a copy of K.  ``changes`` is in
    priority order: on conflicting edits/moves the earlier browser wins."""
    Kn = copy_tree(K, keep_ids=False)
    for role in ROLES:
        Kn.setdefault(role, Node.folder(role))
    res = MergeResult(Kn)

    # Map snapshot ids to K nodes for every browser *before* mutating K.
    maps: list[dict[str, Node]] = []
    for ch in changes:
        S = {r: n for r, n in ch.S.items() if r in Kn}
        m = match_trees(Kn, S)
        mp: dict[str, Node] = {}
        for root in S.values():
            for n in root.walk():
                t = m.c2t.get(id(n))
                if t is not None and n.id is not None:
                    mp[n.id] = t
        maps.append(mp)

    kparent: dict[int, Node] = {}

    def rebuild_parents() -> None:
        kparent.clear()
        for root in Kn.values():
            for n in root.walk():
                for c in n.children:
                    kparent[id(c)] = n

    rebuild_parents()
    updated_by: dict[int, str] = {}
    moved_by: dict[int, str] = {}
    created_by: dict[int, str] = {}
    touched: dict[int, set[str]] = {}
    deletes: list[tuple[str, Node, str]] = []

    def touch(n: Node, browser: str) -> None:
        touched.setdefault(id(n), set()).add(browser)

    def attached(n: Node) -> bool:
        while True:
            p = kparent.get(id(n))
            if p is None:
                return any(n is r for r in Kn.values())
            n = p

    for ch, sid2k in zip(changes, maps):
        b = ch.browser
        st = Stats()
        res.stats[b] = st
        sn, sp, _ = _index(ch.S)
        cn, cp, _ = _index(ch.C)
        cid2k = dict(sid2k)
        roles = [r for r in ch.C if r in Kn]
        root_ids: set[str] = set()
        for r in roles:
            if ch.C[r].id is not None:
                cid2k[ch.C[r].id] = Kn[r]
                root_ids.add(ch.C[r].id)
        for r in ch.S.values():
            if r.id is not None:
                root_ids.add(r.id)

        # -- updates --------------------------------------------------------
        for i, n in cn.items():
            if i in root_ids or i not in sn or n.kind == "separator":
                continue
            o = sn[i]
            if (o.title, o.url) == (n.title, n.url):
                continue
            k = sid2k.get(i)
            if k is None:
                res.log.append(f"{b}: update of {n.title!r} skipped (target not in K)")
                continue
            other = updated_by.get(id(k))
            if other is not None and other != b:
                if (k.title, k.url) != (n.title, n.url):
                    res.conflicts.append(
                        f"{b}: edit of {k.title!r} to {n.title!r} lost to {other}"
                    )
                continue
            k.title = n.title
            if k.kind == "bookmark":
                k.url = n.url
            updated_by[id(k)] = b
            touch(k, b)
            st.updated += 1

        # -- structure: creates and moves ------------------------------------
        def resolve_folder(cf: Node, role: str) -> Node | None:
            k = cid2k.get(cf.id) if cf.id is not None else None
            if k is not None:
                return k if attached(k) else None
            # existing-but-unmapped folder: rebuild its path by titles
            chain = [cf]
            p = cp.get(cf.id)
            while p is not None and cid2k.get(p.id) is None:
                chain.append(p)
                p = cp.get(p.id)
            base = cid2k.get(p.id) if p is not None else Kn.get(role)
            if base is None or not attached(base):
                return None
            for f in reversed(chain):
                nxt = next((x for x in base.children if x.is_folder and x.title == f.title), None)
                if nxt is None:
                    nxt = Node.folder(f.title)
                    base.children.append(nxt)
                    kparent[id(nxt)] = base
                    created_by[id(nxt)] = b
                    st.created += 1
                touch(nxt, b)
                cid2k[f.id] = nxt
                base = nxt
            res.log.append(f"{b}: rebuilt missing folder path for {cf.title!r}")
            return base

        def insert_after(kf: Node, node: Node, siblings: list[Node], pos: int) -> None:
            anchor = None
            for j in range(pos - 1, -1, -1):
                a = cid2k.get(siblings[j].id)
                if a is not None and kparent.get(id(a)) is kf:
                    anchor = a
                    break
            idx = 0 if anchor is None else kf.children.index(anchor) + 1
            kf.children.insert(idx, node)
            kparent[id(node)] = kf

        for role in roles:
            for cf in ch.C[role].walk():
                if not cf.is_folder:
                    continue
                kids = cf.children
                if not kids:
                    continue
                s_kids = sn[cf.id].children if cf.id in sn and cf.id is not None else []
                s_pos = {x.id: j for j, x in enumerate(s_kids)}
                common = [(j, s_pos[x.id]) for j, x in enumerate(kids) if x.id in s_pos]
                stable = {common[q][0] for q in _lis([p for _, p in common])}
                needs = [j for j in range(len(kids)) if j not in stable]
                if not needs:
                    continue
                kf = Kn[role] if cf is ch.C[role] else resolve_folder(cf, role)
                if kf is None:
                    res.log.append(f"{b}: changes inside {cf.title!r} skipped (folder not in K)")
                    continue
                for j in needs:
                    x = kids[j]
                    if x.id not in sn:  # created
                        dup = next(
                            (
                                y
                                for y in kf.children
                                if created_by.get(id(y)) not in (None, b) and y.key() == x.key()
                                and not any(v is y for v in cid2k.values())
                            ),
                            None,
                        )
                        if dup is not None:
                            cid2k[x.id] = dup
                            touch(dup, b)
                            continue
                        k = Node(x.kind, x.title, x.url if x.kind == "bookmark" else None, [])
                        insert_after(kf, k, kids, j)
                        cid2k[x.id] = k
                        created_by[id(k)] = b
                        touch(k, b)
                        st.created += 1
                    else:  # moved or reordered
                        k = cid2k.get(x.id)
                        if k is None or not attached(k):
                            res.log.append(f"{b}: move of {x.title!r} skipped (target not in K)")
                            continue
                        other = moved_by.get(id(k))
                        if other is not None and other != b:
                            res.conflicts.append(f"{b}: move of {k.title!r} lost to {other}")
                            continue
                        if _contains(k, kf):
                            res.conflicts.append(f"{b}: move of {k.title!r} into itself skipped")
                            continue
                        old = kparent.get(id(k))
                        if old is None:
                            continue
                        old.children.remove(k)
                        insert_after(kf, k, kids, j)
                        moved_by[id(k)] = b
                        touch(k, b)
                        st.moved += 1

        # -- deletes (applied after all browsers) ---------------------------
        for i, n in sn.items():
            if i in cn or i in root_ids:
                continue
            p = sp.get(i)
            if p is not None and p.id not in cn and p.id not in root_ids:
                continue  # an ancestor is deleted too
            k = sid2k.get(i)
            st.deleted += sum(1 for _ in n.walk())
            if k is None:
                continue
            deletes.append((b, k, n.title))

    rebuild_parents()
    for b, k, title in deletes:
        p = kparent.get(id(k))
        if p is None or not attached(k):
            continue
        others = set()
        for x in k.walk():
            others |= touched.get(id(x), set()) - {b}
        if others:
            res.conflicts.append(
                f"{b}: delete of {title!r} skipped; edited by {', '.join(sorted(others))} (edit beats delete)"
            )
            continue
        p.children.remove(k)
        for x in k.walk():
            kparent.pop(id(x), None)
    return res


# -- root mapping -------------------------------------------------------------


def apply_inbox(K: Tree, inbox: dict[str, str]) -> int:
    """Move everything in each inbox role to the end of its destination folder.
    Returns the number of items moved."""
    moved = 0
    for role, dest in inbox.items():
        if not dest or role not in K or dest.split("/", 1)[0] == role:
            continue
        src = K[role]
        if not src.children:
            continue
        target = find_path(K, dest, create=True)
        if target is None:
            continue
        target.children.extend(src.children)
        moved += len(src.children)
        src.children = []
    return moved


# -- first run ----------------------------------------------------------------


def union_into(K: Tree, C: Tree) -> int:
    """Union-merge a lifted browser tree into K in place (no deletions).
    Children match by key and occurrence; unmatched items are copied in after
    their nearest matched predecessor.  Returns the number of nodes added."""
    added = 0

    def merge_folder(kf: Node, cf: Node) -> None:
        nonlocal added
        avail: dict[tuple, list[Node]] = {}
        for x in kf.children:
            avail.setdefault(x.key(), []).append(x)
        used: dict[tuple, int] = {}
        anchor: Node | None = None
        pairs: list[tuple[Node, Node]] = []
        for x in cf.children:
            k = x.key()
            lst = avail.get(k, [])
            u = used.get(k, 0)
            if u < len(lst):
                used[k] = u + 1
                anchor = lst[u]
                if x.is_folder:
                    pairs.append((lst[u], x))
            else:
                new = x.copy(keep_ids=False)
                idx = 0 if anchor is None else kf.children.index(anchor) + 1
                kf.children.insert(idx, new)
                anchor = new
                added += sum(1 for _ in new.walk())
        for a, b in pairs:
            merge_folder(a, b)

    for role, root in C.items():
        if role not in K:
            K[role] = Node.folder(role)
        merge_folder(K[role], root)
    return added


def union(trees: list[Tree]) -> Tree:
    K: Tree = {r: Node.folder(r) for r in ROLES}
    for t in trees:
        union_into(K, t)
    return K


def from_source(C: Tree) -> Tree:
    K: Tree = {r: Node.folder(r) for r in ROLES}
    for role, root in C.items():
        K[role] = root.copy(keep_ids=False)
        K[role].title = role
    return K
