"""Property tests: random trees, random concurrent edits, repeated cycles."""

import random

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sim import Sim, SimBrowser, random_edit, random_node
from markstile.engine.cycle import Settings, matches_projection
from markstile.engine.lift import lift
from markstile.engine.model import copy_tree, trees_equal
from markstile.engine.reconcile import apply_ops, reconcile
from markstile.engine.safety import Thresholds

LAX = Thresholds(max_fraction=2.0, max_count=10**9, hold_on_empty=False)


def build(seed: int, strategy: str) -> tuple[Sim, random.Random]:
    rng = random.Random(seed)
    bs = [SimBrowser("chrome"), SimBrowser("edge"), SimBrowser("firefox")]
    for b in bs:
        for _ in range(rng.randint(0, 6)):
            f = rng.choice(b.folders())
            b.add(f, random_node(rng))
    sim = Sim(bs, Settings(thresholds=LAX))
    if strategy == "union":
        sim.first_run("union")
    else:
        sim.first_run("source", rng.choice(list(sim.browsers)))
    return sim, rng


def assert_converged(sim: Sim) -> None:
    for b in sim.browsers.values():
        if b.connected:
            assert matches_projection(sim.K, b.tree, b.profile), b.name


def assert_idempotent(sim: Sim) -> None:
    before = copy_tree(sim.K)
    plan = sim.cycle()
    assert not plan.changed, (before, plan.K)
    for name, p in plan.browsers.items():
        assert p.ops == [], (name, p.ops)


@settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(seed=st.integers(0, 2**32), strategy=st.sampled_from(["union", "source"]),
       rounds=st.integers(1, 4), offline=st.booleans())
def test_convergence_and_idempotence(seed, strategy, rounds, offline):
    sim, rng = build(seed, strategy)
    assert_converged(sim)
    assert_idempotent(sim)
    for _ in range(rounds):
        if offline:
            for b in sim.browsers.values():
                b.connected = rng.random() > 0.3
        for b in sim.browsers.values():
            for _ in range(rng.randint(0, 4)):
                random_edit(b, rng)
        sim.cycle()
        assert_converged(sim)
    for b in sim.browsers.values():
        b.connected = True
    sim.cycle()
    assert_converged(sim)
    assert_idempotent(sim)


@settings(deadline=None)
@given(seed=st.integers(0, 2**32), editor=st.sampled_from(["chrome", "edge", "firefox"]))
def test_single_editor_is_preserved(seed, editor):
    """When only one browser changes, its edits survive exactly: it gets no ops."""
    sim, rng = build(seed, "source")
    b = sim.browsers[editor]
    for _ in range(rng.randint(1, 6)):
        random_edit(b, rng)
    edited = copy_tree(b.tree)
    plan = sim.cycle()
    assert plan.browsers[editor].ops == [], plan.browsers[editor].ops
    assert trees_equal(b.tree, edited)
    assert_converged(sim)


@settings(deadline=None)
@given(seed=st.integers(0, 2**32))
def test_reconcile_reaches_target(seed):
    rng = random.Random(seed)
    a, t = SimBrowser("firefox"), SimBrowser("firefox")
    for b in (a, t):
        for _ in range(rng.randint(0, 10)):
            b.add(rng.choice(b.folders()), random_node(rng))
    # derive target partly from a so that matching has something to find
    target = copy_tree(a.tree)
    tb = SimBrowser("firefox")
    tb.tree = target
    for _ in range(rng.randint(0, 8)):
        random_edit(tb, rng)
    ops = reconcile(copy_tree(target, keep_ids=False), a.tree)
    cur = copy_tree(a.tree)
    apply_ops(cur, ops, id_prefix="new")
    assert trees_equal(cur, target)
    assert reconcile(copy_tree(target, keep_ids=False), cur) == []
