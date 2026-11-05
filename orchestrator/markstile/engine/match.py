"""Content matching between two trees.

Used both by the reconciler (``K`` projection vs a browser's current tree) and
to map a browser snapshot's IDs onto canonical nodes.  Matching runs in three
passes that repeat until nothing new pairs up:

1. *local*: within each matched folder pair, children pair by key
   ``(type, title, url)``; the k-th occurrence of a key pairs with the k-th.
2. *global*: leftovers anywhere in the tree pair by key, which turns a
   delete+create of the same item into a move.
3. *heuristic*: leftovers within one folder pair as renames (bookmark with the
   same URL or title; folder with mostly the same children, or the sole
   leftover folder on each side).
"""

from __future__ import annotations

from collections import Counter, defaultdict

from .model import Node, Tree


class Matching:
    def __init__(self) -> None:
        self.t2c: dict[int, Node] = {}
        self.c2t: dict[int, Node] = {}
        self.folder_pairs: list[tuple[Node, Node]] = []

    def pair(self, t: Node, c: Node) -> None:
        self.t2c[id(t)] = c
        self.c2t[id(c)] = t
        if t.is_folder:
            self.folder_pairs.append((t, c))

    def get(self, t: Node) -> Node | None:
        return self.t2c.get(id(t))

    def c_matched(self, c: Node) -> bool:
        return id(c) in self.c2t


def _local(m: Matching, t: Node, c: Node) -> None:
    """Pair children of a matched folder pair by key, then recurse."""
    stack = [(t, c)]
    while stack:
        tf, cf = stack.pop()
        avail: dict[tuple, list[Node]] = defaultdict(list)
        for cc in cf.children:
            if not m.c_matched(cc):
                avail[cc.key()].append(cc)
        used: Counter = Counter()
        for tc in tf.children:
            if m.get(tc) is not None:
                continue
            k = tc.key()
            lst = avail.get(k)
            if lst and used[k] < len(lst):
                cc = lst[used[k]]
                used[k] += 1
                m.pair(tc, cc)
                if tc.is_folder:
                    stack.append((tc, cc))


def _unmatched(m: Matching, roots: list[Node], side: str) -> list[Node]:
    out: list[Node] = []
    for r in roots:
        for n in r.walk():
            if n is r:
                continue
            if side == "t" and m.get(n) is None:
                out.append(n)
            elif side == "c" and not m.c_matched(n):
                out.append(n)
    return out


def _global(m: Matching, troots: list[Node], croots: list[Node]) -> bool:
    pool: dict[tuple, list[Node]] = defaultdict(list)
    for c in _unmatched(m, croots, "c"):
        if c.kind != "separator":
            pool[c.key()].append(c)
    changed = False
    for t in _unmatched(m, troots, "t"):
        if t.kind == "separator" or m.get(t) is not None:
            continue
        lst = pool.get(t.key())
        while lst and m.c_matched(lst[0]):
            lst.pop(0)
        if lst:
            c = lst.pop(0)
            m.pair(t, c)
            if t.is_folder:
                _local(m, t, c)
            changed = True
    return changed


def _overlap(a: Node, b: Node) -> float:
    if not a.children or not b.children:
        return 0.0
    ca = Counter(x.key() for x in a.children)
    cb = Counter(x.key() for x in b.children)
    common = sum((ca & cb).values())
    return common / max(len(a.children), len(b.children))


def _heuristic(m: Matching) -> bool:
    changed = False
    for tf, cf in list(m.folder_pairs):
        ts = [x for x in tf.children if m.get(x) is None and x.kind != "separator"]
        cs = [x for x in cf.children if not m.c_matched(x) and x.kind != "separator"]
        if not ts or not cs:
            continue
        # bookmarks: same url, then same title
        for attr in ("url", "title"):
            for t in ts:
                if t.kind != "bookmark" or m.get(t) is not None:
                    continue
                for c in cs:
                    if c.kind == "bookmark" and not m.c_matched(c) and getattr(c, attr) == getattr(t, attr):
                        m.pair(t, c)
                        changed = True
                        break
        # folders: content overlap
        tfs = [x for x in ts if x.is_folder and m.get(x) is None]
        cfs = [x for x in cs if x.is_folder and not m.c_matched(x)]
        for t in tfs:
            best, score = None, 0.5
            for c in cfs:
                if m.c_matched(c):
                    continue
                s = _overlap(t, c)
                if s >= score:
                    best, score = c, s
            if best is not None:
                m.pair(t, best)
                _local(m, t, best)
                changed = True
        tfs = [x for x in tfs if m.get(x) is None]
        cfs = [x for x in cfs if not m.c_matched(x)]
        if len(tfs) == 1 and len(cfs) == 1:
            m.pair(tfs[0], cfs[0])
            _local(m, tfs[0], cfs[0])
            changed = True
    return changed


def match_trees(T: Tree, C: Tree) -> Matching:
    """Match target tree T against current tree C (roots pair by role)."""
    m = Matching()
    roles = [r for r in T if r in C]
    for r in roles:
        m.pair(T[r], C[r])
        _local(m, T[r], C[r])
    troots = [T[r] for r in roles]
    croots = [C[r] for r in roles]
    while True:
        a = _global(m, troots, croots)
        b = _heuristic(m)
        if not (a or b):
            break
    return m
