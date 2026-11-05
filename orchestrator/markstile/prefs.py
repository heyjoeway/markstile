"""Browser settings that no policy covers, written into a profile's
Preferences file just before the browser starts (``markstile prefs``).

Only unprotected preferences belong here: Chromium signs some of them (see
``protection.macs`` in a profile) and resets any that are edited from outside.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Vivaldi: new tabs (and a new window's first tab) open about:blank instead of
# its Start Page (vivaldi.tabs.new_page.link: 0 start page, 1 home page,
# 2 blank page; see resources/vivaldi/prefs_definitions.json).
PREFS = {
    "vivaldi": {"vivaldi": {"tabs": {"new_page": {"link": 2}}}},
}


def _merge(into: dict, values: dict) -> None:
    for k, v in values.items():
        if isinstance(v, dict) and isinstance(into.get(k), dict):
            _merge(into[k], v)
        else:
            into[k] = v


def apply(browser: str, profile: Path) -> bool:
    """Write the browser's settings into ``profile`` (its --user-data-dir).
    The browser must not be running.  Returns False if there was nothing to do
    or the Preferences file couldn't be read."""
    values = PREFS.get(browser)
    if not values:
        return False
    f = profile / "Default" / "Preferences"
    try:
        prefs = json.loads(f.read_text()) if f.exists() else {}
    except (OSError, ValueError):
        return False  # leave a file the browser can't read either alone
    _merge(prefs, values)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name("Preferences.markstile")
    tmp.write_text(json.dumps(prefs))
    os.replace(tmp, f)
    return True


def main(argv: list[str]) -> None:
    if len(argv) != 2:
        raise SystemExit("usage: markstile prefs <browser> <profile dir>")
    apply(argv[0], Path(argv[1]))


if __name__ == "__main__":
    main(sys.argv[1:])
