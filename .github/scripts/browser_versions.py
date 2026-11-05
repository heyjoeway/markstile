#!/usr/bin/env python3
"""Print the version of each browser the image installs, as JSON, read from
the same apt repositories the Dockerfile uses (keep the two lists in step).

The image workflow compares this with the label on the last published image
and only rebuilds when a browser has a new release.
"""

import gzip
import json
import sys
import urllib.request

# package: (repository, suite, component), as in docker/Dockerfile
REPOS = {
    "google-chrome-stable": ("https://dl.google.com/linux/chrome/deb", "stable", "main"),
    "microsoft-edge-stable": ("https://packages.microsoft.com/repos/edge", "stable", "main"),
    "firefox-devedition": ("https://packages.mozilla.org/apt", "mozilla", "main"),
    "brave-browser": ("https://brave-browser-apt-release.s3.brave.com", "stable", "main"),
    "vivaldi-stable": ("https://repo.vivaldi.com/archive/deb", "stable", "main"),
    "opera-stable": ("https://deb.opera.com/opera-stable", "stable", "non-free"),
}


def fetch(url: str) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": "markstile-version-check"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    except OSError:
        return None


def packages_index(repo: str, suite: str, component: str) -> str:
    base = f"{repo}/dists/{suite}/{component}/binary-amd64/Packages"
    for suffix, decode in (("", lambda b: b), (".gz", gzip.decompress)):
        data = fetch(base + suffix)
        if data:
            return decode(data).decode("utf-8", "replace")
    raise RuntimeError(f"no package index at {base}")


def newest(index: str, package: str) -> str:
    """The newest version of ``package`` in an apt Packages index."""
    versions = []
    for stanza in index.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in stanza.splitlines() if ": " in line and not line[0].isspace())
        if fields.get("Package") == package and "Version" in fields:
            versions.append(fields["Version"])
    if not versions:
        raise RuntimeError(f"{package} not found in its repository")
    return max(versions, key=_version_key)


def _version_key(v: str) -> list:
    # good enough for browser versions (dotted numbers with a -N revision)
    parts = []
    for chunk in v.replace("-", ".").replace("~", ".").split("."):
        parts.append((0, int(chunk), "") if chunk.isdigit() else (1, 0, chunk))
    return parts


def main() -> None:
    out = {}
    for package, (repo, suite, component) in REPOS.items():
        out[package] = newest(packages_index(repo, suite, component), package)
    json.dump(out, sys.stdout, sort_keys=True, separators=(",", ":"))
    print()


if __name__ == "__main__":
    main()
