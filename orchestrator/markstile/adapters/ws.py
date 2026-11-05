"""Localhost WebSocket server the browser extensions connect out to.

The same server also answers plain HTTP for the Chrome/Edge extension update
manifest and CRX (``/ext/update.xml``, ``/ext/markstile.crx``), so everything a
browser needs binds to 127.0.0.1 only.
"""

from __future__ import annotations

import asyncio
import hmac
import itertools
import json
import logging
from http import HTTPStatus
from pathlib import Path

from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Response

from ..browsers import BROWSERS
from .base import Registry

log = logging.getLogger("markstile.ws")

PING_INTERVAL = 20.0
REQUEST_TIMEOUT = 120.0


class RemoteError(Exception):
    pass


class WsSession:
    def __init__(self, conn: ServerConnection, browser: str, ext_version: str | None, browser_version: str | None,
                 boot_id: str | None = None):
        self.conn = conn
        self.boot_id = boot_id
        self.browser = browser
        self.ext_version = ext_version
        self.browser_version = browser_version
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}

    async def request(self, method: str, params: dict | None = None, timeout: float = REQUEST_TIMEOUT):
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self.conn.send(json.dumps({"type": "request", "id": rid, "method": method, "params": params or {}}))
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    def resolve(self, msg: dict) -> None:
        fut = self._pending.get(msg.get("id"))
        if fut is None or fut.done():
            return
        if "error" in msg:
            fut.set_exception(RemoteError(str(msg["error"])))
        else:
            fut.set_result(msg.get("result"))

    def fail_all(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)

    async def get_tree(self):
        return await self.request("getTree")

    async def apply_ops(self, ops: list[dict]) -> list[dict]:
        return await self.request("applyOps", {"ops": ops}, timeout=max(REQUEST_TIMEOUT, len(ops) * 0.5))


class ExtensionServer:
    def __init__(self, registry: Registry, secret: str, host: str = "127.0.0.1", port: int = 8765,
                 ext_dir: Path | None = None) -> None:
        self.registry = registry
        self.secret = secret
        self.host = host
        self.port = port
        self.ext_dir = ext_dir
        self._server = None
        # called with the browser id when an extension has no secret yet
        self.on_unconfigured = None

    async def start(self) -> None:
        self._server = await serve(
            self._handler, self.host, self.port, process_request=self._http, ping_interval=None, max_size=64 * 2**20
        )
        if self.port == 0:
            self.port = self._server.sockets[0].getsockname()[1]
        log.info("extension server on ws://%s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # -- HTTP: extension update server ------------------------------------------
    def _http(self, conn: ServerConnection, request):
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return None
        path = request.path.split("?", 1)[0]
        files = {"/ext/update.xml": ("update.xml", "text/xml"), "/ext/markstile.crx": ("markstile.crx", "application/x-chrome-extension")}
        if self.ext_dir and path in files:
            name, ctype = files[path]
            f = self.ext_dir / name
            if f.exists():
                body = f.read_bytes()
                return Response(HTTPStatus.OK.value, "OK", Headers({"Content-Type": ctype, "Content-Length": str(len(body)), "Cache-Control": "no-store"}), body)
        return Response(HTTPStatus.NOT_FOUND.value, "Not Found", Headers({"Content-Length": "0"}), b"")

    # -- WebSocket ---------------------------------------------------------------
    async def _handler(self, conn: ServerConnection) -> None:
        try:
            raw = await asyncio.wait_for(conn.recv(), 10)
            hello = json.loads(raw)
        except Exception:
            await conn.close(1008, "expected hello")
            return
        browser = hello.get("browser")
        if hello.get("type") != "hello" or browser not in BROWSERS:
            await conn.close(1008, "bad hello")
            return
        if self.secret and not hello.get("secret"):
            # the browser hasn't handed the extension its managed settings yet
            log.debug("rejected %s extension: no secret yet", browser)
            if self.on_unconfigured:
                self.on_unconfigured(browser)
            await conn.close(1008, "no secret")
            return
        if self.secret and not hmac.compare_digest(str(hello.get("secret", "")), self.secret):
            log.warning("rejected %s extension: bad secret", browser)
            await conn.close(1008, "bad secret")
            return
        old = self.registry.sessions.get(browser)
        if isinstance(old, WsSession):
            await old.conn.close(1000, "replaced")
        sess = WsSession(conn, browser, hello.get("extVersion"), hello.get("browserVersion"), hello.get("bootId"))
        await self.registry.add(sess)
        pinger = asyncio.create_task(self._pinger(conn))
        try:
            async for raw in conn:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                t = msg.get("type")
                if t == "response":
                    sess.resolve(msg)
                elif t == "event":
                    await self.registry.event(browser, msg)
        except Exception as e:  # connection dropped
            log.info("%s connection closed: %s", browser, e)
        finally:
            pinger.cancel()
            sess.fail_all(ConnectionError(f"{browser} disconnected"))
            await self.registry.remove(sess)

    async def _pinger(self, conn: ServerConnection) -> None:
        # Application-level pings keep the MV3 service worker alive.
        while True:
            await asyncio.sleep(PING_INTERVAL)
            try:
                await conn.send('{"type":"ping"}')
            except Exception:
                return
