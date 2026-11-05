"""Browser session interface shared by the WebSocket bridge and the fake adapter."""

from __future__ import annotations

from typing import Awaitable, Callable, Protocol


class BrowserSession(Protocol):
    browser: str
    ext_version: str | None
    browser_version: str | None

    async def get_tree(self) -> list | dict: ...

    async def apply_ops(self, ops: list[dict]) -> list[dict]: ...


EventCallback = Callable[[str, dict], Awaitable[None] | None]
ConnectCallback = Callable[[str, bool], Awaitable[None] | None]


class Registry:
    """Currently connected browser sessions, by browser name."""

    def __init__(self) -> None:
        self.sessions: dict[str, BrowserSession] = {}
        self.on_event: EventCallback | None = None
        self.on_connect: ConnectCallback | None = None

    async def _call(self, cb, *args) -> None:
        if cb is None:
            return
        r = cb(*args)
        if r is not None and hasattr(r, "__await__"):
            await r

    async def add(self, s: BrowserSession) -> None:
        self.sessions[s.browser] = s
        await self._call(self.on_connect, s.browser, True)

    async def remove(self, s: BrowserSession) -> None:
        if self.sessions.get(s.browser) is s:
            del self.sessions[s.browser]
            await self._call(self.on_connect, s.browser, False)

    async def event(self, browser: str, ev: dict) -> None:
        await self._call(self.on_event, browser, ev)

    def connected(self) -> list[str]:
        return sorted(self.sessions)
