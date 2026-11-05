"""Pure planning of one sync cycle (steps 2–5 of the design's sync cycle).

The orchestrator fetches trees, calls :func:`plan_cycle`, sends each browser
its ops, re-fetches to verify, and commits.  Nothing here does I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .lift import Profile, lift, project
from .merge import BrowserChange, MergeResult, apply_inbox, diff_stats, merge, union_into
from .model import Tree, copy_tree, trees_equal
from .reconcile import reconcile
from .safety import Assessment, Thresholds, assess

Mode = Literal["normal", "approved", "rejected"]


@dataclass
class BrowserInput:
    browser: str
    profile: Profile
    current: Tree  # native, with ids
    snapshot: Tree | None  # native, with ids; None -> join
    mode: Mode = "normal"


@dataclass
class BrowserPlan:
    browser: str
    ops: list[dict] = field(default_factory=list)
    held: Assessment | None = None
    joined: int | None = None  # nodes added to K when joining
    reconcile: bool = True


@dataclass
class CyclePlan:
    K: Tree
    changed: bool
    browsers: dict[str, BrowserPlan]
    merge: MergeResult | None
    inbox_moved: int = 0

    @property
    def log(self) -> list[str]:
        return self.merge.log if self.merge else []

    @property
    def conflicts(self) -> list[str]:
        return self.merge.conflicts if self.merge else []


@dataclass
class Settings:
    thresholds: Thresholds = field(default_factory=Thresholds)
    inbox: dict[str, str] = field(default_factory=dict)  # role -> destination path
    priority: list[str] = field(default_factory=list)  # browser names, highest first


def plan_cycle(K: Tree, inputs: list[BrowserInput], settings: Settings) -> CyclePlan:
    plans = {i.browser: BrowserPlan(i.browser) for i in inputs}
    order = {b: n for n, b in enumerate(settings.priority)}
    ordered = sorted(inputs, key=lambda i: order.get(i.browser, len(order)))

    changes: list[BrowserChange] = []
    joins: list[BrowserInput] = []
    for inp in ordered:
        if inp.mode == "rejected":  # reconcile to K only, contribute nothing
            continue
        if inp.snapshot is None:
            joins.append(inp)
            continue
        S = lift(inp.snapshot, inp.profile)
        C = lift(inp.current, inp.profile)
        if inp.mode != "approved":
            a = assess(S, C, settings.thresholds)
            if a.hold:
                plans[inp.browser].held = a
                plans[inp.browser].reconcile = False
                continue
        if diff_stats(S, C).total():
            changes.append(BrowserChange(inp.browser, S, C))

    if changes:
        mr = merge(K, changes)
        newK = mr.K
    else:
        mr = None
        newK = copy_tree(K, keep_ids=False)
    for inp in joins:
        plans[inp.browser].joined = union_into(newK, lift(inp.current, inp.profile))
    moved = apply_inbox(newK, settings.inbox)

    for inp in inputs:
        p = plans[inp.browser]
        if p.reconcile:
            p.ops = reconcile(project(newK, inp.profile, inp.current), inp.current)
    return CyclePlan(newK, not trees_equal(K, newK), plans, mr, moved)


def matches_projection(K: Tree, current: Tree, profile: Profile) -> bool:
    proj = project(K, profile, current)
    cur = {r: n for r, n in current.items() if r in proj}
    return trees_equal(proj, cur)
