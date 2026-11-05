"""The sync runner: fetch -> plan (pure engine) -> apply -> verify -> commit."""

from __future__ import annotations

import asyncio
import logging
import time

from .adapters.base import Registry
from .browsers import BROWSERS, is_vendor_default, label
from .engine.cycle import BrowserInput, Settings, matches_projection, plan_cycle
from .engine.lift import Profile, lift, parse_native, project
from .engine.merge import from_source, union
from .engine.model import Node, Tree, count_bookmarks
from .engine.reconcile import reconcile
from .engine.safety import Thresholds
from .store import Store

log = logging.getLogger("markstile.sync")

SUPPRESS_SECONDS = 2.0
# Chromium writes its Bookmarks file ~2.5 s after the last change, so a browser
# that dies within seconds of our writes can come back without them.
UNFLUSHED_SECONDS = 10.0


def removed_bookmarks(current: Tree, ops: list[dict]) -> int:
    idx = {n.id: n for root in current.values() for n in root.walk()}
    return sum(
        idx[o["id"]].count_bookmarks() if idx[o["id"]].is_folder else 1
        for o in ops
        if o["op"] in ("remove", "removeTree") and o["id"] in idx and idx[o["id"]].kind != "separator"
    )


class Syncer:
    def __init__(self, store: Store, registry: Registry) -> None:
        self.store = store
        self.registry = registry
        self.lock = asyncio.Lock()
        self.suppress_until: dict[str, float] = {}
        self.last_cycle: dict | None = None
        self.running = False

    # -- connection tracking --------------------------------------------------------
    def on_connect(self, browser: str, connected: bool) -> None:
        """Call on extension connect/disconnect (before the scheduler reacts)."""
        now = time.time()
        if not connected:
            self.store.set(f"lastseen.{browser}", now)
            return
        boot = getattr(self.registry.sessions.get(browser), "boot_id", None)
        if not boot:
            return
        prev = self.store.get(f"boot.{browser}")
        self.store.set(f"boot.{browser}", boot)
        if prev is None or prev == boot:
            return
        # The browser restarted.  If it went away right after we wrote to it,
        # those writes may never have reached disk: diffing against the snapshot
        # would read them as deletions.  Rejoin by union instead (no deletes).
        last_write = self.store.get(f"lastwrite.{browser}", 0)
        last_seen = self.store.get(f"lastseen.{browser}", 0)
        if last_write and last_seen - last_write < UNFLUSHED_SECONDS:
            self.store.delete_snapshot(browser)
            self.store.log("warning", browser, "browser restarted right after a sync; rejoining by union")

    # -- first contact --------------------------------------------------------------
    async def first_contact(self, browser: str) -> None:
        """Run the first-contact check for a newly connected browser (on its own
        task: the session can't answer requests until its handler returns)."""
        if self._needs_first_contact(browser):
            async with self.lock:
                await self._first_contact(browser)

    def _needs_first_contact(self, browser: str) -> bool:
        spec = BROWSERS.get(browser)
        return bool(spec and spec.default_hosts) and self.store.get(f"firstcontact.{browser}") is None

    async def _first_contact(self, browser: str) -> None:
        """Empty a brand-new profile that holds only the vendor's default
        (sponsored) bookmarks, before anything syncs them.  Done once per
        browser: whatever arrives later, e.g. through vendor sync, is kept."""
        if not self._needs_first_contact(browser) or browser not in self.registry.sessions:
            return
        native = await self._fetch(browser)
        if native is None:
            return
        marks = [n for root in native.values() for n in root.walk() if n.kind == "bookmark"]
        if marks and all(is_vendor_default(browser, n.url) for n in marks):
            ops = [{"op": "removeTree" if c.is_folder else "remove", "id": c.id}
                   for root in native.values() for c in root.children]
            errors = await self._apply(browser, ops)
            after = await self._fetch(browser)
            left = sum(len(r.children) for r in after.values()) if after is not None else len(ops)
            if errors or left:
                self._error(browser, f"could not remove all default bookmarks ({left} items left): {(errors or ['no error reported'])[0]}")
                return
            self.store.log("info", browser, f"removed the {len(marks)} default bookmarks {label(browser)} put into its new profile")
        self.store.set(f"firstcontact.{browser}", time.time())

    # -- helpers ----------------------------------------------------------------
    def profile(self, browser: str, native: Tree) -> Profile:
        s = self.store.settings()
        return Profile.for_browser(browser, native.keys(), {"menu": s.menu_folder})

    def engine_settings(self) -> Settings:
        s = self.store.settings()
        return Settings(Thresholds(s.hold_fraction, s.hold_count), s.inbox(), list(s.priority))

    def suppressed(self, browser: str) -> bool:
        return time.monotonic() < self.suppress_until.get(browser, 0)

    async def _fetch(self, browser: str) -> Tree | None:
        sess = self.registry.sessions.get(browser)
        if sess is None:
            return None
        try:
            native = parse_native(await sess.get_tree(), browser)
        except Exception as e:
            self._error(browser, f"getTree failed: {e}")
            return None
        if not native:
            self._error(browser, "no bookmark roots found in getTree() result")
            return None
        self.store.set(f"lastseen.{browser}", time.time())
        self.store.update_browser(
            browser, bookmarks=count_bookmarks(native),
            ext_version=getattr(sess, "ext_version", None), browser_version=getattr(sess, "browser_version", None),
        )
        return native

    def enabled(self) -> list[str]:
        """Browsers that take part in syncing (chosen and not paused)."""
        s = self.store.settings()
        return [b for b in s.browsers or [] if b not in s.paused]

    async def _fetch_all(self) -> dict[str, Tree]:
        out = {}
        enabled = set(self.enabled())
        for b in self.registry.connected():
            if b not in enabled:
                continue
            await self._first_contact(b)
            t = await self._fetch(b)
            if t is not None:
                out[b] = t
        return out

    def _error(self, browser: str | None, msg: str) -> None:
        log.error("%s: %s", browser or "sync", msg)
        self.store.log("error", browser or "sync", msg)
        if browser:
            self.store.update_browser(browser, last_error=msg)

    async def _apply(self, browser: str, ops: list[dict]) -> list[str]:
        sess = self.registry.sessions.get(browser)
        if sess is None:
            raise ConnectionError(f"{browser} disconnected")
        self.suppress_until[browser] = float("inf")
        try:
            self.store.set(f"lastwrite.{browser}", time.time())
            results = await sess.apply_ops(ops)
        finally:
            self.suppress_until[browser] = time.monotonic() + SUPPRESS_SECONDS
        return [f"{o['op']} {o.get('id') or o.get('title')}: {r.get('error')}"
                for o, r in zip(ops, results or []) if not r.get("ok")]

    async def _push(self, browser: str, K: Tree, current: Tree, ops: list[dict]) -> tuple[Tree | None, bool]:
        """Apply ops, re-fetch and verify; retry the reconcile once on mismatch.
        Returns (tree after writes or None if unknown, verified)."""
        for attempt in range(2):
            if ops:
                errs = await self._apply(browser, ops)
                for e in errs[:20]:
                    self.store.log("warning", browser, f"op failed: {e}")
            after = await self._fetch(browser)
            if after is None:
                return None, False
            # roles can appear or vanish (Chromium's mobile root): profile the new tree
            prof = self.profile(browser, after)
            if matches_projection(K, after, prof):
                return after, True
            ops = reconcile(project(K, prof, after), after)
            if attempt == 0:
                self.store.log("warning", browser, f"verify mismatch, retrying with {len(ops)} ops")
        return after, False

    # -- the cycle ----------------------------------------------------------------
    async def run_cycle(self, reason: str = "manual") -> dict:
        async with self.lock:
            self.running = True
            try:
                return await self._cycle(reason)
            except Exception as e:  # never kill the scheduler
                log.exception("cycle failed")
                self._error(None, f"cycle failed: {e!r}")
                return {"error": str(e)}
            finally:
                self.running = False

    async def _cycle(self, reason: str) -> dict:
        started = time.time()
        settings = self.store.settings()
        currents = await self._fetch_all()
        result: dict = {"ts": started, "reason": reason, "browsers": sorted(currents)}
        if not settings.sync_enabled:
            result["skipped"] = "waiting for first-run merge choice"
            self.last_cycle = result
            return result
        cur = self.store.current_k()
        if cur is None:
            result["skipped"] = "no canonical tree"
            self.last_cycle = result
            return result
        _, K = cur
        inputs = []
        for b, native in currents.items():
            st = self.store.browser(b)
            inputs.append(BrowserInput(b, self.profile(b, native), native, self.store.snapshot(b), st.mode))
        plan = plan_cycle(K, inputs, self.engine_settings())

        for msg in plan.log:
            self.store.log("info", "merge", msg)
        for msg in plan.conflicts:
            self.store.log("warning", "conflict", msg)
        if plan.inbox_moved:
            self.store.log("info", "inbox", f"moved {plan.inbox_moved} item(s) out of inbox roots")

        if plan.changed:
            summary = {
                "reason": reason,
                "browsers": {b: s.as_dict() for b, s in (plan.merge.stats.items() if plan.merge else [])},
                "joined": {b: p.joined for b, p in plan.browsers.items() if p.joined},
                "conflicts": len(plan.conflicts),
                "inbox_moved": plan.inbox_moved,
                "bookmarks": count_bookmarks(plan.K),
            }
            vid = self.store.add_version(plan.K, summary, reason)
            result["version"] = vid

        result["held"] = {}
        result["ops"] = {}
        for b, p in plan.browsers.items():
            if p.held is not None:
                hold = {"reason": p.held.reason, "deleted": p.held.deleted, "total": p.held.total, "ts": time.time()}
                prev = self.store.browser(b).hold
                if not prev or prev.get("reason") != hold["reason"]:
                    self.store.log("warning", b, f"changes held: {p.held.reason}")
                self.store.update_browser(b, hold=hold)
                result["held"][b] = hold
                continue
            result["ops"][b] = len(p.ops)
            try:
                after, ok = await self._push(b, plan.K, currents[b], p.ops)
            except Exception as e:
                self._error(b, f"apply failed: {e}")
                after, ok = None, False
            if after is None:
                # state after our writes is unknown: rejoin (union, no deletions) next time
                self.store.delete_snapshot(b)
                continue
            self.store.save_snapshot(b, after)
            if ok:
                self.store.update_browser(b, mode="normal", hold=None, last_error=None, last_sync=time.time())
            else:
                self._error(b, "browser does not match the canonical tree after reconcile")
                self.store.update_browser(b, mode="normal", hold=None)
        s = self.store.settings()
        self.store.prune_versions(s.retention_days, s.retention_versions)
        self.store.prune_logs()
        result["changed"] = plan.changed
        result["duration"] = round(time.time() - started, 3)
        if plan.changed or any(result["ops"].values()) or result["held"]:
            self.store.log(
                "info", "cycle",
                f"{reason}: K {'changed' if plan.changed else 'unchanged'}; ops "
                + ", ".join(f"{b}={n}" for b, n in result["ops"].items())
                + (f"; held: {', '.join(result['held'])}" if result["held"] else ""),
            )
        self.last_cycle = result
        return result

    # -- first run ------------------------------------------------------------------
    async def first_run_preview(self) -> dict:
        async with self.lock:
            currents = await self._fetch_all()
        counts = {b: count_bookmarks(t) for b, t in currents.items()}
        removals: dict[str, dict[str, int]] = {}
        for src, st in currents.items():
            K = from_source(lift(st, self.profile(src, st)))
            removals[src] = {
                b: removed_bookmarks(t, reconcile(project(K, self.profile(b, t), t), t))
                for b, t in currents.items() if b != src
            }
        Ku = union([lift(t, self.profile(b, t)) for b, t in currents.items()])
        return {"counts": counts, "removals": removals, "union_count": count_bookmarks(Ku)}

    async def first_run(self, strategy: str, source: str | None = None) -> dict:
        async with self.lock:
            currents = await self._fetch_all()
            if not currents:
                raise ValueError("no browsers connected")
            if strategy == "union":
                K = union([lift(t, self.profile(b, t)) for b, t in currents.items()])
                label = "first run: union"
            elif strategy == "source" and source in currents:
                K = from_source(lift(currents[source], self.profile(source, currents[source])))
                label = f"first run: from {source}"
            else:
                raise ValueError("invalid strategy or source browser not connected")
            self.store.add_version(K, {"reason": label, "bookmarks": count_bookmarks(K)}, label)
            self.store.clear_snapshots()
            for b in currents:
                self.store.update_browser(b, mode="rejected", hold=None)
            s = self.store.settings()
            s.sync_enabled = True
            self.store.save_settings(s)
            self.store.log("info", "first-run", label)
        return await self.run_cycle(label)

    async def set_browsers(self, browsers: list[str]) -> tuple[list[str], list[str]]:
        """Change which browsers run and sync.  Returns (added, removed).  Once
        sync is enabled an added browser is paused until join(); a removed one
        is left as it is."""
        async with self.lock:
            s = self.store.settings()
            old = list(s.browsers or [])
            s.browsers = list(browsers)
            added = [b for b in browsers if b not in old]
            removed = [b for b in old if b not in browsers]
            # once syncing, a new browser waits until it is signed in (see join)
            s.paused = [b for b in s.paused if b in browsers] + (added if s.sync_enabled else [])
            self.store.save_settings(s)
            for b in added + removed:
                self.store.delete_snapshot(b)
                self.store.update_browser(b, mode="normal", hold=None, last_error=None)
            if added or removed:
                self.store.log("info", "browsers", "synced browsers: " + ", ".join(browsers))
        return added, removed

    async def join(self, browser: str) -> dict:
        """Start syncing a paused browser: it joins by union, deleting nothing."""
        async with self.lock:
            s = self.store.settings()
            if browser not in s.paused:
                raise ValueError("that browser is not waiting to join")
            s.paused.remove(browser)
            self.store.save_settings(s)
            self.store.delete_snapshot(browser)
            self.store.update_browser(browser, mode="normal", hold=None, last_error=None)
            self.store.log("info", browser, "started syncing; joining by union")
        return await self.run_cycle(f"{browser} joined")

    # -- holds / restore -------------------------------------------------------------
    async def resolve_hold(self, browser: str, approve: bool) -> dict:
        self.store.update_browser(browser, mode="approved" if approve else "rejected", hold=None)
        self.store.log("info", browser, f"held changes {'approved' if approve else 'rejected'}")
        return await self.run_cycle(f"hold {'approved' if approve else 'rejected'} ({browser})")

    async def restore(self, version_id: int) -> dict:
        K = self.store.version(version_id)
        if K is None:
            raise ValueError("no such version")
        label = f"restore of version {version_id}"
        async with self.lock:
            self.store.add_version(K, {"reason": label, "bookmarks": count_bookmarks(K)}, label)
            for st in self.store.browsers():
                self.store.update_browser(st.browser, mode="rejected", hold=None)
            self.store.log("warning", "restore", label)
        return await self.run_cycle(label)

    async def reset_first_run(self) -> None:
        """Back to the first-run state (keeps history)."""
        async with self.lock:
            s = self.store.settings()
            s.sync_enabled = False
            self.store.save_settings(s)
            self.store.clear_snapshots()


__all__ = ["Syncer", "Node"]
