"""When to sync: debounced bookmark events, a fixed interval, manual requests,
and a startup cycle once the extensions have connected."""

from __future__ import annotations

import asyncio
import logging
import time

from .sync import Syncer

log = logging.getLogger("markstile.scheduler")


class Scheduler:
    def __init__(self, syncer: Syncer, quiet: float = 5.0, max_wait: float = 30.0,
                 startup_timeout: float = 60.0) -> None:
        self.syncer = syncer
        self.quiet = quiet
        self.max_wait = max_wait
        self.startup_timeout = startup_timeout
        self._wake = asyncio.Event()
        self._first_event: float | None = None
        self._last_event: float | None = None
        self._manual: str | None = None
        self._started_at = time.monotonic()
        self._startup_done = False
        self.next_scheduled: float | None = None  # wall clock, for the dashboard
        self._task: asyncio.Task | None = None

    def interval(self) -> float:
        return max(30, int(self.syncer.store.settings().sync_interval))

    # -- triggers -----------------------------------------------------------------
    def on_event(self, browser: str, ev: dict) -> None:
        if self.syncer.suppressed(browser):
            return
        now = time.monotonic()
        if self._first_event is None:
            self._first_event = now
        self._last_event = now
        self._wake.set()

    def on_connect(self, browser: str, connected: bool) -> None:
        if connected and self._startup_done:
            self.on_event(browser, {"kind": "connected"})  # catch up after a reconnect
        self._wake.set()

    def request(self, reason: str = "manual") -> None:
        self._manual = reason
        self._wake.set()

    # -- loop -------------------------------------------------------------------------
    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def _due(self, now: float) -> tuple[float, str]:
        if self._manual:
            return now, self._manual
        if not self._startup_done:
            connected = set(self.syncer.registry.connected())
            expected = set(self.syncer.enabled())
            if expected and expected <= connected:
                return now, "startup"
            return self._started_at + self.startup_timeout, "startup"
        cands = [(self._next_interval, "scheduled")]
        if self._first_event is not None:
            cands.append((min(self._last_event + self.quiet, self._first_event + self.max_wait), "bookmark events"))
        return min(cands)

    async def _loop(self) -> None:
        self._next_interval = time.monotonic() + self.interval()
        while True:
            self._wake.clear()
            now = time.monotonic()
            due, reason = self._due(now)
            self.next_scheduled = time.time() + (self._next_interval - now) if self._startup_done else None
            if due > now:
                try:
                    await asyncio.wait_for(self._wake.wait(), due - now)
                    continue
                except asyncio.TimeoutError:
                    pass
            self._manual = None
            self._first_event = self._last_event = None
            self._startup_done = True
            self._next_interval = time.monotonic() + self.interval()
            log.info("sync cycle: %s", reason)
            await self.syncer.run_cycle(reason)
