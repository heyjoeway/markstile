"""Mass-deletion hold."""

from __future__ import annotations

from dataclasses import dataclass

from .model import Tree


@dataclass
class Thresholds:
    max_fraction: float = 0.20
    max_count: int = 50
    hold_on_empty: bool = True


@dataclass
class Assessment:
    hold: bool
    deleted: int
    total: int
    reason: str = ""


def _bookmark_ids(tree: Tree) -> set[str]:
    return {n.id for root in tree.values() for n in root.walk() if n.kind == "bookmark" and n.id is not None}


def assess(S: Tree, C: Tree, th: Thresholds) -> Assessment:
    """Compare lifted snapshot and current tree of one browser."""
    before = _bookmark_ids(S)
    after = _bookmark_ids(C)
    deleted = len(before - after)
    total = len(before)
    if th.hold_on_empty and total and not after:
        return Assessment(True, deleted, total, "bookmark tree became empty")
    if deleted > th.max_count:
        return Assessment(True, deleted, total, f"{deleted} bookmarks deleted (limit {th.max_count})")
    if total and deleted / total > th.max_fraction:
        return Assessment(
            True, deleted, total, f"{deleted} of {total} bookmarks deleted (over {th.max_fraction:.0%})"
        )
    return Assessment(False, deleted, total)
