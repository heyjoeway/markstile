"""Start and stop the browser programs to match the portal's browser choice.

The browsers are supervisord programs with ``autostart=false``; the
orchestrator starts the chosen ones (and stops the rest) through
``supervisorctl``.  It also writes the list the console's desktop menu shows,
and erases the profile of a browser the user removes.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from .browsers import BROWSERS, label, ordered

log = logging.getLogger("markstile.procs")


class BrowserProcesses:
    def __init__(self, supervisorctl: list[str] | None = None, menu_file: Path | None = None,
                 profiles: Path | None = None) -> None:
        self.supervisorctl = supervisorctl  # None: nothing to control (demo, tests)
        self.menu_file = menu_file
        self.profiles = profiles  # one --user-data-dir / Firefox profile per browser id
        self.running: set[str] = set()

    async def apply(self, enabled: list[str] | None) -> list[str]:
        """Run exactly the enabled browsers.  Returns error messages."""
        enabled = ordered(enabled or [])
        self._write_menu(enabled)
        # in parallel: each start waits a few seconds for the browser to stay up
        results = await asyncio.gather(*(
            self._ctl("start" if b in enabled else "stop", b) if self.supervisorctl is not None else _ok()
            for b in BROWSERS
        ))
        errors = []
        for b, err in zip(BROWSERS, results):
            if err:
                errors.append(err)
            else:
                (self.running.add if b in enabled else self.running.discard)(b)
        return errors

    async def restart(self, enabled: list[str]) -> list[str]:
        """Restart the enabled browsers one after another, so that they don't
        all start up at once.  Returns error messages."""
        if self.supervisorctl is None:
            return []
        errors = []
        for b in ordered(enabled):
            if err := await self._ctl("restart", b):
                errors.append(err)
        return errors

    async def erase_profiles(self, browsers: list[str]) -> tuple[list[str], list[str]]:
        """Empty the profiles of stopped browsers: bookmarks, sign-in and
        settings go, and the browser starts fresh if it's added again.
        Returns (erased browsers, error messages)."""
        if self.profiles is None:
            return [], []
        erased, errors = [], []
        for b in browsers:
            if b not in BROWSERS:
                continue
            if b in self.running:
                errors.append(f"kept the {label(b)} profile: the browser didn't stop")
                continue
            try:
                await asyncio.to_thread(self._erase, b)
                erased.append(b)
            except OSError as e:
                errors.append(f"could not erase the {label(b)} profile: {e}")
        return erased, errors

    def _erase(self, browser: str) -> None:
        d = self.profiles / browser
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        if browser == "firefox":
            from .provision import USER_JS

            (d / "user.js").write_text(USER_JS)

    async def _ctl(self, action: str, program: str) -> str | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.supervisorctl, action, program,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), 60)
        except (OSError, asyncio.TimeoutError) as e:
            return f"{action} {program}: {e}"
        text = out.decode(errors="replace").strip()
        # "already started" / "not running" are fine: the state is what we want
        if proc.returncode != 0 and "already started" not in text and "not running" not in text:
            log.error("supervisorctl %s %s: %s", action, program, text)
            return f"{action} {program}: {text}"
        return None

    def _write_menu(self, enabled: list[str]) -> None:
        if self.menu_file is None:
            return
        try:
            tmp = self.menu_file.with_suffix(".tmp")
            tmp.write_text("".join(f"{b}\t{label(b)}\n" for b in enabled))
            tmp.replace(self.menu_file)
        except OSError as e:
            log.warning("could not write %s: %s", self.menu_file, e)


async def _ok() -> None:
    return None
