"""Scheduled browser restarts, to shed whatever memory the browsers leak over
days of uptime.  A restart waits until nobody has used the console for a
while, so it never pulls a browser out from under the user."""

from __future__ import annotations

import asyncio
import logging
from time import monotonic, time as wall_clock

from .browsers import label, ordered
from .procs import BrowserProcesses
from .store import Store
from .sync import Syncer

log = logging.getLogger("markstile.restarts")

CONSOLE_IDLE = 5 * 60  # seconds without console input before a restart may run
CHECK_EVERY = 60
# On a cold start Chrome can install the extension without handing it its
# managed settings (the secret) until the browser restarts.
UNCONFIGURED_FOR = 15  # seconds of secret-less connection attempts before restarting
UNCONFIGURED_RETRY = 10 * 60  # at most one such restart per browser this often

# RFB client-to-server message types that mean a person is at the console
# (noVNC also keeps requesting screen updates, which don't count).
_KEY_EVENT, _POINTER_EVENT = 4, 5


class ConsoleActivity:
    """When someone last used the console (monotonic clock)."""

    def __init__(self) -> None:
        self.last_input = float("-inf")

    def opened(self) -> None:
        self.last_input = monotonic()

    def client_message(self, data: bytes) -> None:
        # noVNC sends each message batch as one WebSocket message, so the first
        # byte is a message type.  (A handshake message can look like input
        # once; that only delays a restart.)
        if data and data[0] in (_KEY_EVENT, _POINTER_EVENT):
            self.last_input = monotonic()

    def idle_for(self) -> float:
        return monotonic() - self.last_input


class BrowserRestarter:
    def __init__(self, store: Store, procs: BrowserProcesses, syncer: Syncer, console: ConsoleActivity) -> None:
        self.store = store
        self.procs = procs
        self.syncer = syncer
        self.console = console
        # The browsers start with the container, which counts as a restart.
        self.last_restart = wall_clock()
        self._task: asyncio.Task | None = None
        self._unconfigured_since: dict[str, tuple[float, float]] = {}  # first, latest attempt
        self._healed_at: dict[str, float] = {}

    def due_at(self) -> float | None:
        """Wall-clock time the next restart is due, or None when turned off."""
        hours = self.store.settings().restart_hours
        return self.last_restart + hours * 3600 if hours > 0 else None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(CHECK_EVERY)
            try:
                await self.check()
            except Exception:  # keep the schedule alive
                log.exception("scheduled restart failed")

    def extension_unconfigured(self, browser: str) -> None:
        """The browser's extension connected without its secret.  Restart the
        browser once that has gone on for a while."""
        now = monotonic()
        since, last = self._unconfigured_since.get(browser, (now, now))
        if now - last > 60:  # an earlier episode that ended by itself
            since = now
        self._unconfigured_since[browser] = (since, now)
        if now - since < UNCONFIGURED_FOR or now - self._healed_at.get(browser, float("-inf")) < UNCONFIGURED_RETRY:
            return
        if browser not in (self.store.settings().browsers or []):
            return
        self._healed_at[browser] = now
        self._unconfigured_since.pop(browser, None)
        asyncio.create_task(self._heal(browser))

    async def _heal(self, browser: str) -> None:
        self.store.log("info", "browsers", f"restarting {label(browser)}: its extension hasn't received its settings")
        for err in await self.procs.restart([browser]):
            self.store.log("error", "browsers", err)

    async def check(self) -> bool:
        """Restart the browsers if they are due and the console is idle."""
        due = self.due_at()
        if due is None or wall_clock() < due or self.console.idle_for() < CONSOLE_IDLE:
            return False
        browsers = ordered(self.store.settings().browsers or [])
        # Not in the middle of a sync cycle; the extensions reconnect afterwards
        # and the scheduler catches up on anything that changed meanwhile.
        async with self.syncer.lock:
            errors = await self.procs.restart(browsers)
        self.last_restart = wall_clock()
        for err in errors:
            self.store.log("error", "browsers", err)
        if browsers:
            names = ", ".join(label(b) for b in browsers)
            self.store.log("info", "browsers", f"scheduled restart: {names}")
        return True
