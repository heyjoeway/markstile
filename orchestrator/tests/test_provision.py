import json
import struct
import zipfile
import io
import subprocess
import sys
from pathlib import Path

from markstile import crx
from markstile.provision import FIREFOX_EXT_ID, provision

ROOT = Path(__file__).resolve().parents[2]


def test_provision_writes_everything(tmp_path):
    ext = tmp_path / "ext"
    subprocess.run([sys.executable, str(ROOT / "extension" / "build.py"), str(ext)], check=True, capture_output=True)
    ff = tmp_path / "firefox"
    ff.mkdir()
    info = provision(tmp_path / "data", tmp_path / "run", ext, 8765, ff, tmp_path / "etc")
    ext_id = info["extension_id"]
    assert len(ext_id) == 32 and set(ext_id) <= set("abcdefghijklmnop")

    pol = json.loads((tmp_path / "etc/opt/chrome/policies/managed/markstile.json").read_text())
    assert pol["ExtensionInstallForcelist"] == [f"{ext_id};http://127.0.0.1:8765/ext/update.xml"]
    secret = (tmp_path / "run/ext.secret").read_text()
    assert pol["3rdparty"]["extensions"][ext_id] == {"wsPort": 8765, "secret": secret, "browser": "chrome"}
    edge = json.loads((tmp_path / "etc/opt/edge/policies/managed/markstile.json").read_text())
    assert edge["3rdparty"]["extensions"][ext_id]["browser"] == "edge"
    brave = json.loads((tmp_path / "etc/brave/policies/managed/markstile.json").read_text())
    assert brave["3rdparty"]["extensions"][ext_id]["browser"] == "brave"
    assert brave["ExtensionInstallForcelist"] == pol["ExtensionInstallForcelist"]
    assert brave["BraveRewardsDisabled"] is True and "bookmarks" not in brave["SyncTypesListDisabled"]
    for browser, d in (("vivaldi", "vivaldi"), ("opera", "opt/opera")):
        p = json.loads((tmp_path / "etc" / d / "policies/managed/markstile.json").read_text())
        assert p["3rdparty"]["extensions"][ext_id]["browser"] == browser
        assert p["ExtensionInstallForcelist"] == pol["ExtensionInstallForcelist"]
        assert p["RestoreOnStartup"] == 4 and p["RestoreOnStartupURLs"] == ["about:blank"]

    # debloat policies never switch off the one thing the container is for
    assert "bookmarks" not in pol["SyncTypesListDisabled"] and "favorites" not in edge["SyncTypesListDisabled"]
    assert pol["NewTabPageLocation"] == edge["NewTabPageLocation"] == "about:blank"
    assert "PromotionsEnabled" not in edge  # Chrome-only

    ffp = json.loads((ff / "distribution/policies.json").read_text())["policies"]
    assert ffp["NewTabPage"] is False and "services.sync" not in (ff / "mozilla.cfg").read_text()
    assert ffp["ExtensionSettings"][FIREFOX_EXT_ID]["installation_mode"] == "force_installed"
    for old in ("stileaf@stileaf.local", "stilite@stilite.local"):
        assert ffp["ExtensionSettings"][old]["installation_mode"] == "blocked"
    assert ffp["3rdparty"]["Extensions"][FIREFOX_EXT_ID]["secret"] == secret
    assert (ff / "mozilla.cfg").read_text().startswith("//")
    assert "xpinstall.signatures.required" in (tmp_path / "data/profiles/firefox/user.js").read_text()

    # CRX3 structure and update manifest
    blob = (tmp_path / "run/ext/markstile.crx").read_bytes()
    assert blob[:4] == b"Cr24"
    version, hlen = struct.unpack("<II", blob[4:12])
    assert version == 3
    z = zipfile.ZipFile(io.BytesIO(blob[12 + hlen:]))
    assert set(z.namelist()) == {"manifest.json", "background.js", "schema.json", "icon-128.png"}
    assert json.loads(z.read("manifest.json"))["update_url"] == "http://127.0.0.1:8765/ext/update.xml"
    assert ext_id in (tmp_path / "run/ext/update.xml").read_text()

    # the key is kept: same id on the next start, new secret
    again = provision(tmp_path / "data", tmp_path / "run", ext, 8765, ff, tmp_path / "etc")
    assert again["extension_id"] == ext_id
    assert (tmp_path / "run/ext.secret").read_text() != secret


def test_crx_signature_verifies(tmp_path):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.serialization import load_der_public_key

    src = tmp_path / "src"
    src.mkdir()
    (src / "manifest.json").write_text("{}")
    key = crx.load_or_create_key(tmp_path / "k.pem")
    blob = crx.pack_crx3(src, key)
    hlen = struct.unpack("<I", blob[8:12])[0]
    header, archive = blob[12:12 + hlen], blob[12 + hlen:]
    pub = crx.public_der(key)
    sig = header[header.index(pub) + len(pub) + 3:]  # field 2 tag + 2-byte length follow the key
    sig = sig[:256]
    shd = b"\x0a\x10" + __import__("hashlib").sha256(pub).digest()[:16]
    msg = b"CRX3 SignedData\x00" + struct.pack("<I", len(shd)) + shd + archive
    load_der_public_key(pub).verify(sig, msg, padding.PKCS1v15(), hashes.SHA256())
    assert header.endswith(b"\x82\xf1\x04\x12" + shd)
