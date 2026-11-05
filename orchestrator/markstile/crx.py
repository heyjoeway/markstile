"""CRX3 packing with a locally generated key (never a maintainer key)."""

from __future__ import annotations

import hashlib
import io
import struct
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


def load_or_create_key(path: Path) -> rsa.RSAPrivateKey:
    if path.exists():
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    path.chmod(0o600)
    return key


def public_der(key: rsa.RSAPrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def extension_id(pub_der: bytes) -> str:
    digest = hashlib.sha256(pub_der).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _field(num: int, data: bytes) -> bytes:
    return _varint((num << 3) | 2) + _varint(len(data)) + data


def zip_dir(src: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(src).as_posix())
    return buf.getvalue()


def pack_crx3(src_dir: Path, key: rsa.RSAPrivateKey) -> bytes:
    """Pack an unpacked extension directory into CRX3 bytes."""
    archive = zip_dir(src_dir)
    pub = public_der(key)
    crx_id = hashlib.sha256(pub).digest()[:16]
    signed_header_data = _field(1, crx_id)  # SignedData { bytes crx_id = 1; }
    to_sign = b"CRX3 SignedData\x00" + struct.pack("<I", len(signed_header_data)) + signed_header_data + archive
    sig = key.sign(to_sign, padding.PKCS1v15(), hashes.SHA256())
    proof = _field(1, pub) + _field(2, sig)  # AsymmetricKeyProof
    header = _field(2, proof) + _field(10000, signed_header_data)  # CrxFileHeader
    return b"Cr24" + struct.pack("<II", 3, len(header)) + header + archive


def update_manifest(ext_id: str, version: str, crx_url: str) -> str:
    return (
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<gupdate xmlns='http://www.google.com/update2/response' protocol='2.0'>\n"
        f"  <app appid='{ext_id}'>\n"
        f"    <updatecheck codebase='{crx_url}' version='{version}' />\n"
        "  </app>\n"
        "</gupdate>\n"
    )
