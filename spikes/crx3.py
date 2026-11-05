#!/usr/bin/env python3
"""Minimal CRX3 packer (stdlib + openssl CLI only).

Usage:
  crx3.py genkey KEY.pem                      # 2048-bit RSA key (PKCS#8 PEM)
  crx3.py id KEY.pem                          # print extension ID
  crx3.py pack SRC_DIR KEY.pem OUT.crx        # zip SRC_DIR and pack as CRX3
  crx3.py update-xml ID VERSION CRX_URL       # print gupdate update manifest

CRX3 layout (see chromium components/crx_file/crx3.proto):
  "Cr24" | uint32le 3 | uint32le header_len | CrxFileHeader | zip
  CrxFileHeader {
    repeated AsymmetricKeyProof sha256_with_rsa = 2;   // {1: public_key (DER SPKI), 2: signature}
    optional bytes signed_header_data = 10000;          // serialized SignedData {1: crx_id (16 bytes)}
  }
  signature = RSA-PKCS1v1.5-SHA256 over
    b"CRX3 SignedData\\x00" | uint32le len(signed_header_data) | signed_header_data | zip
  crx_id = sha256(SPKI DER)[:16]; extension ID = hex(crx_id) mapped 0-f -> a-p.
"""
import hashlib
import io
import os
import struct
import subprocess
import sys
import tempfile
import zipfile


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


def _field_bytes(field: int, data: bytes) -> bytes:
    # wire type 2 (length-delimited)
    return _varint((field << 3) | 2) + _varint(len(data)) + data


def genkey(key_path: str) -> None:
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt",
                    "rsa_keygen_bits:2048", "-out", key_path], check=True,
                   stderr=subprocess.DEVNULL)
    os.chmod(key_path, 0o600)


def public_key_der(key_path: str) -> bytes:
    return subprocess.run(["openssl", "pkey", "-in", key_path, "-pubout", "-outform", "DER"],
                          check=True, capture_output=True).stdout


def crx_id(pub_der: bytes) -> bytes:
    return hashlib.sha256(pub_der).digest()[:16]


def extension_id(pub_der: bytes) -> str:
    return "".join(chr(ord("a") + int(c, 16)) for c in crx_id(pub_der).hex())


def _sign(key_path: str, data: bytes) -> bytes:
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(data)
        tmp = f.name
    try:
        return subprocess.run(["openssl", "dgst", "-sha256", "-sign", key_path, tmp],
                              check=True, capture_output=True).stdout
    finally:
        os.unlink(tmp)


def zip_dir(src: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(src):
            dirs.sort()
            for name in sorted(files):
                full = os.path.join(root, name)
                arc = os.path.relpath(full, src).replace(os.sep, "/")
                zi = zipfile.ZipInfo(arc, date_time=(2020, 1, 1, 0, 0, 0))
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = 0o644 << 16
                with open(full, "rb") as fh:
                    z.writestr(zi, fh.read())
    return buf.getvalue()


def pack(zip_bytes: bytes, key_path: str) -> bytes:
    pub = public_key_der(key_path)
    signed_data = _field_bytes(1, crx_id(pub))
    to_sign = b"CRX3 SignedData\x00" + struct.pack("<I", len(signed_data)) + signed_data + zip_bytes
    sig = _sign(key_path, to_sign)
    proof = _field_bytes(1, pub) + _field_bytes(2, sig)
    header = _field_bytes(2, proof) + _field_bytes(10000, signed_data)
    return b"Cr24" + struct.pack("<II", 3, len(header)) + header + zip_bytes


def update_xml(ext_id: str, version: str, crx_url: str) -> str:
    return ("<?xml version='1.0' encoding='UTF-8'?>\n"
            "<gupdate xmlns='http://www.google.com/update2/response' protocol='2.0'>\n"
            f"  <app appid='{ext_id}'>\n"
            f"    <updatecheck codebase='{crx_url}' version='{version}' />\n"
            "  </app>\n</gupdate>\n")


def main(argv):
    if len(argv) >= 2 and argv[0] == "genkey":
        genkey(argv[1])
    elif len(argv) >= 2 and argv[0] == "id":
        print(extension_id(public_key_der(argv[1])))
    elif len(argv) >= 4 and argv[0] == "pack":
        data = pack(zip_dir(argv[1]), argv[2])
        with open(argv[3], "wb") as f:
            f.write(data)
        print(extension_id(public_key_der(argv[2])))
    elif len(argv) >= 4 and argv[0] == "update-xml":
        sys.stdout.write(update_xml(argv[1], argv[2], argv[3]))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
