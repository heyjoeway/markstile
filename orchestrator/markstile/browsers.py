"""The browsers this image can run.  Adding one means an entry here, a
``[program:<id>]`` in supervisord.conf, a case in ``docker/bin/launch`` and its
policies in ``provision.py``."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class BrowserSpec:
    id: str  # also the supervisord program name and the extension's "browser"
    label: str
    engine: str  # "chromium" or "gecko": decides the bookmark-tree dialect
    # Hosts of the bookmarks the browser puts into a new profile.  A profile
    # holding nothing else when MarkStile first meets it is emptied (see Syncer).
    default_hosts: tuple[str, ...] = ()


BROWSERS: dict[str, BrowserSpec] = {
    b.id: b
    for b in (
        BrowserSpec("chrome", "Google Chrome", "chromium"),
        BrowserSpec("edge", "Microsoft Edge", "chromium"),
        BrowserSpec("brave", "Brave", "chromium"),
        BrowserSpec("vivaldi", "Vivaldi", "chromium", default_hosts=("vivaldi.com",)),
        BrowserSpec("opera", "Opera", "chromium"),
        BrowserSpec("firefox", "Firefox Developer Edition", "gecko", default_hosts=("mozilla.org",)),
    )
}


def is_gecko(browser: str) -> bool:
    spec = BROWSERS.get(browser)
    return spec is not None and spec.engine == "gecko"


def is_vendor_default(browser: str, url: str | None) -> bool:
    spec = BROWSERS.get(browser)
    host = urlsplit(url or "").hostname or ""
    return spec is not None and any(host == h or host.endswith("." + h) for h in spec.default_hosts)


def label(browser: str) -> str:
    spec = BROWSERS.get(browser)
    return spec.label if spec else browser


def ordered(browsers) -> list[str]:
    """Known browsers in registry order, unknown ones dropped."""
    chosen = set(browsers)
    return [b for b in BROWSERS if b in chosen]
