#!/usr/bin/env python3
"""Spike test server: static HTTP (update.xml + crx) and a WebSocket endpoint.

Run with the project venv (needs `websockets`):
  .venv/bin/python spikes/spike_server.py --serve DIR --out OUTDIR [--http-port 8780] [--ws-port 8765]

Every connection: logs hello, requests getTree once (saved to OUTDIR/tree-<browser>.json),
optionally runs --script requests then saves OUTDIR/tree-<browser>-after.json,
pings every --ping-interval seconds, logs every event with timestamps to OUTDIR/events.log.
"""
import argparse
import asyncio
import functools
import http.server
import itertools
import json
import os
import threading
import time

import websockets

ARGS = None
_ids = itertools.count(1)


def log(msg: str, **kw):
    line = json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "msg": msg, **kw})
    print(line, flush=True)
    with open(os.path.join(ARGS.out, "events.log"), "a") as f:
        f.write(line + "\n")


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".crx": "application/x-chrome-extension", ".xml": "text/xml",
                      ".xpi": "application/x-xpinstall"}

    def log_message(self, fmt, *a):
        log("http", req=self.requestline, ua=self.headers.get("User-Agent", "")[:80])

    def end_headers(self):
        # Without this, Chrome heuristically caches update.xml (Last-Modified) and never re-fetches it.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_POST(self):  # some update clients POST; answer with the same static file
        self.do_GET()


async def ws_handler(conn):
    peer = conn.remote_address
    t0 = time.time()
    info = {}
    pending = {}

    async def request(kind, **extra):
        rid = next(_ids)
        fut = asyncio.get_running_loop().create_future()
        pending[rid] = fut
        await conn.send(json.dumps({"type": kind, "id": rid, **extra}))
        return await asyncio.wait_for(fut, 30)

    async def pinger():
        while True:
            await asyncio.sleep(ARGS.ping_interval)
            r = await request("ping")
            log("pong", browser=info.get("browser"), uptimeMs=r.get("uptimeMs"),
                connects=r.get("connects"), connectedFor=round(time.time() - t0))

    async def after_hello():
        r = await request("getTree")
        path = os.path.join(ARGS.out, f"tree-{info.get('browser')}.json")
        with open(path, "w") as f:
            json.dump(r, f, indent=2)
        log("tree saved", path=path, ok=r.get("ok"))
        if ARGS.script:
            with open(ARGS.script) as f:
                steps = json.load(f)
            results = []
            for step in steps:
                if step["type"] == "sleep":  # server-side delay, not sent to the extension
                    await asyncio.sleep(step["seconds"])
                    continue
                def subst(v):  # "$ref:N" -> id of the node returned by step N
                    if isinstance(v, str) and v.startswith("$ref:"):
                        r = results[int(v[5:])]
                        val = r.get("value") or r.get("node") or {}
                        return val.get("id") if isinstance(val, dict) else val
                    if isinstance(v, list):
                        return [subst(x) for x in v]
                    if isinstance(v, dict):
                        return {k: subst(x) for k, x in v.items()}
                    return v
                extra = subst({k: v for k, v in step.items() if k != "type"})
                results.append(await request(step["type"], **extra))
            r2 = await request("getTree")
            path = os.path.join(ARGS.out, f"tree-{info.get('browser')}-after.json")
            with open(path, "w") as f:
                json.dump({"steps": steps, "results": results, "tree": r2.get("tree")}, f, indent=2)
            log("script done", path=path, results=[x.get("ok") for x in results])

    tasks = []
    try:
        async for raw in conn:
            msg = json.loads(raw)
            if msg.get("type") == "hello":
                info = msg
                log("hello", peer=str(peer), **msg)
                tasks += [asyncio.create_task(after_hello()), asyncio.create_task(pinger())]
            elif msg.get("id") in pending:
                pending.pop(msg["id"]).set_result(msg)
            else:
                log("msg", browser=info.get("browser"), data=msg)
    except Exception as e:  # noqa
        log("ws error", err=repr(e))
    finally:
        for t in tasks:
            t.cancel()
        log("disconnect", browser=info.get("browser"), connectedFor=round(time.time() - t0))


async def main():
    global ARGS
    p = argparse.ArgumentParser()
    p.add_argument("--serve", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--http-port", type=int, default=8780)
    p.add_argument("--ws-port", type=int, default=8765)
    p.add_argument("--script", help="JSON list of requests to send after the first getTree")
    p.add_argument("--ping-interval", type=float, default=20)
    ARGS = p.parse_args()
    os.makedirs(ARGS.out, exist_ok=True)
    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", ARGS.http_port), functools.partial(Handler, directory=ARGS.serve))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log("listening", http=ARGS.http_port, ws=ARGS.ws_port)
    async with websockets.serve(ws_handler, "127.0.0.1", ARGS.ws_port, ping_interval=None):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
