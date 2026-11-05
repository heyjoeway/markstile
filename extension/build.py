#!/usr/bin/env python3
"""Build the two extension packages from the shared source.

    build.py <outdir>

Produces ``<outdir>/chrome/`` (unpacked MV3 extension; packed into a signed CRX
at container start by ``markstile provision``, since the key is generated per
install) and ``<outdir>/firefox.xpi`` (unsigned; Firefox Developer Edition).
"""

import shutil
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def build(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    chrome = out / "chrome"
    if chrome.exists():
        shutil.rmtree(chrome)
    chrome.mkdir()
    shutil.copy(HERE / "chrome" / "manifest.json", chrome / "manifest.json")
    shutil.copy(HERE / "src" / "background.js", chrome / "background.js")
    shutil.copy(HERE / "src" / "schema.json", chrome / "schema.json")
    shutil.copy(HERE / "src" / "icon-128.png", chrome / "icon-128.png")

    xpi = out / "firefox.xpi"
    with zipfile.ZipFile(xpi, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(HERE / "firefox" / "manifest.json", "manifest.json")
        z.write(HERE / "src" / "background.js", "background.js")
        z.write(HERE / "src" / "icon-128.png", "icon-128.png")
    print(f"built {chrome} and {xpi}")


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else HERE.parent / "build" / "extension"))
