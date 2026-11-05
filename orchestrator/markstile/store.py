"""SQLite persistence: settings, auth, canonical tree versions, per-browser
snapshots and state, held changes, and logs."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .browsers import ordered
from .engine.model import ROLES, Node, Tree, tree_from_json, tree_to_json

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    tree TEXT NOT NULL,
    summary TEXT NOT NULL,
    source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (browser TEXT PRIMARY KEY, ts REAL NOT NULL, tree TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS browsers (
    browser TEXT PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT 'normal',
    last_sync REAL,
    last_error TEXT,
    bookmarks INTEGER,
    ext_version TEXT,
    browser_version TEXT,
    hold TEXT
);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL
);
"""


# Phone bookmarks land in "mobile"; Chromium hides that root while it's empty, so
# mirroring it can't reach every browser.  Move them to a folder everyone shows.
DEFAULT_ROOT_MODES = {"toolbar": "mirror", "other": "mirror", "mobile": "other/From phone", "menu": "mirror"}


@dataclass
class SyncSettings:
    sync_enabled: bool = False
    menu_folder: str = "other/Bookmarks Menu"
    # per role: "mirror" or a destination path ("inbox -> <path>")
    root_modes: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ROOT_MODES))
    hold_fraction: float = 0.20
    hold_count: int = 50
    sync_interval: int = 300
    priority: list[str] = field(default_factory=list)
    retention_days: int = 30
    retention_versions: int = 100
    # restart the browsers this often (once the console is idle); 0: never
    restart_hours: int = 24
    # browser ids to run and sync; None until chosen in the portal
    browsers: list[str] | None = None
    # added after the first run: running, but not synced until the user has
    # signed in to them and says so (the first-run step for one browser)
    paused: list[str] = field(default_factory=list)

    def inbox(self) -> dict[str, str]:
        return {r: m for r, m in self.root_modes.items() if m and m != "mirror"}


@dataclass
class BrowserState:
    browser: str
    mode: str = "normal"
    last_sync: float | None = None
    last_error: str | None = None
    bookmarks: int | None = None
    ext_version: str | None = None
    browser_version: str | None = None
    hold: dict | None = None


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    # -- key/value ----------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def delete(self, key: str) -> None:
        self.db.execute("DELETE FROM kv WHERE key=?", (key,))

    def settings(self) -> SyncSettings:
        raw = self.get("settings", {})
        s = SyncSettings()
        for k, v in raw.items():
            if hasattr(s, k):
                setattr(s, k, v)
        for r in ROLES:
            s.root_modes.setdefault(r, DEFAULT_ROOT_MODES[r])
        if "browsers" not in raw and self.browsers():
            # installs from before the choice existed: keep what they had
            s.browsers = ordered(st.browser for st in self.browsers())
        return s

    def save_settings(self, s: SyncSettings) -> None:
        self.set("settings", asdict(s))

    # -- canonical tree versions ---------------------------------------------
    def current_k(self) -> tuple[int, Tree] | None:
        row = self.db.execute("SELECT id, tree FROM versions ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return None
        return row[0], _normalize(tree_from_json(json.loads(row[1])))

    def add_version(self, K: Tree, summary: dict, source: str) -> int:
        cur = self.db.execute(
            "INSERT INTO versions(ts, tree, summary, source) VALUES(?,?,?,?)",
            (time.time(), json.dumps(tree_to_json(K)), json.dumps(summary), source),
        )
        return cur.lastrowid

    def versions(self, limit: int = 200) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, ts, summary, source FROM versions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [{"id": r[0], "ts": r[1], "summary": json.loads(r[2]), "source": r[3]} for r in rows]

    def version(self, vid: int) -> Tree | None:
        row = self.db.execute("SELECT tree FROM versions WHERE id=?", (vid,)).fetchone()
        return _normalize(tree_from_json(json.loads(row[0]))) if row else None

    def prune_versions(self, days: int, keep: int) -> int:
        """Delete versions older than ``days`` that are not among the last ``keep``."""
        cutoff = time.time() - days * 86400
        cur = self.db.execute(
            "DELETE FROM versions WHERE ts < ? AND id NOT IN (SELECT id FROM versions ORDER BY id DESC LIMIT ?)",
            (cutoff, max(keep, 1)),
        )
        return cur.rowcount

    # -- snapshots -----------------------------------------------------------
    def snapshot(self, browser: str) -> Tree | None:
        row = self.db.execute("SELECT tree FROM snapshots WHERE browser=?", (browser,)).fetchone()
        return tree_from_json(json.loads(row[0])) if row else None

    def save_snapshot(self, browser: str, tree: Tree) -> None:
        self.db.execute(
            "INSERT INTO snapshots(browser, ts, tree) VALUES(?,?,?) "
            "ON CONFLICT(browser) DO UPDATE SET ts=excluded.ts, tree=excluded.tree",
            (browser, time.time(), json.dumps(tree_to_json(tree, with_ids=True))),
        )

    def delete_snapshot(self, browser: str) -> None:
        self.db.execute("DELETE FROM snapshots WHERE browser=?", (browser,))

    def clear_snapshots(self) -> None:
        self.db.execute("DELETE FROM snapshots")

    # -- browser state ---------------------------------------------------------
    def browser(self, name: str) -> BrowserState:
        row = self.db.execute(
            "SELECT browser, mode, last_sync, last_error, bookmarks, ext_version, browser_version, hold "
            "FROM browsers WHERE browser=?",
            (name,),
        ).fetchone()
        if not row:
            return BrowserState(name)
        return BrowserState(*row[:7], json.loads(row[7]) if row[7] else None)

    def update_browser(self, name: str, **fields: Any) -> None:
        st = self.browser(name)
        for k, v in fields.items():
            setattr(st, k, v)
        self.db.execute(
            "INSERT INTO browsers(browser, mode, last_sync, last_error, bookmarks, ext_version, browser_version, hold) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(browser) DO UPDATE SET mode=excluded.mode, "
            "last_sync=excluded.last_sync, last_error=excluded.last_error, bookmarks=excluded.bookmarks, "
            "ext_version=excluded.ext_version, browser_version=excluded.browser_version, hold=excluded.hold",
            (
                name, st.mode, st.last_sync, st.last_error, st.bookmarks,
                st.ext_version, st.browser_version, json.dumps(st.hold) if st.hold else None,
            ),
        )

    def browsers(self) -> list[BrowserState]:
        names = [r[0] for r in self.db.execute("SELECT browser FROM browsers ORDER BY browser")]
        return [self.browser(n) for n in names]

    # -- logs ------------------------------------------------------------------
    def log(self, level: str, kind: str, message: str) -> None:
        self.db.execute(
            "INSERT INTO logs(ts, level, kind, message) VALUES(?,?,?,?)", (time.time(), level, kind, message)
        )

    def logs(self, limit: int = 300) -> list[dict]:
        rows = self.db.execute(
            "SELECT ts, level, kind, message FROM logs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [{"ts": r[0], "level": r[1], "kind": r[2], "message": r[3]} for r in rows]

    def prune_logs(self, keep: int = 5000) -> None:
        self.db.execute("DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT ?)", (keep,))


def _normalize(K: Tree) -> Tree:
    for r in ROLES:
        K.setdefault(r, Node.folder(r))
    return K
