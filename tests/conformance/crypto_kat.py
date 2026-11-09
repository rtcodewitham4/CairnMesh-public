"""Crypto conformance: Wycheproof AES-CCM vectors, an independent CCM, and the spec's byte layout.

* Wycheproof (C2SP/wycheproof ``aes_ccm_test.json``): every vector through ``cryptography``'s
  AESCCM and through ``ccm()`` below (RFC 3610 / SP 800-38C written out on AES-ECB).
* Differential: random inputs at the protocol's parameters (13-byte nonce, 4-byte MIC),
  ``ccm()`` against AESCCM.
* Layout: ``secure.seal`` output rebuilt from the module docstring's formula
  (nonce = SENDER | TARGET | FCNT LE | KIND|0x80 | TX | 5 zero bytes, AAD = first 9 bytes).

    python tests/conformance/crypto_kat.py [N_RANDOM]   # or via tests/conformance/test_crypto_kat.py
"""

from __future__ import annotations

import json
import random
import struct
import sys
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from cairnmesh import secure as S


def _ecb(key: bytes):
    return Cipher(algorithms.AES(key), modes.ECB()).encryptor()


def ccm(key: bytes, nonce: bytes, aad: bytes, msg: bytes, tag_len: int, decrypt: bool = False):
    """AES-CCM; returns ciphertext||tag, or the plaintext (None on a bad tag) when ``decrypt``."""
    L = 15 - len(nonce)
    enc = _ecb(key)

    def block(b):
        return enc.update(b)

    if decrypt:
        if len(msg) < tag_len:
            return None
        ct, tag = msg[:-tag_len], msg[-tag_len:]
    else:
        ct = None
    data = msg if not decrypt else None

    def ctr(i):
        return block(bytes([L - 1]) + nonce + i.to_bytes(L, "big"))

    def xor_stream(text):
        out = bytearray()
        for k in range(0, len(text), 16):
            s = ctr(k // 16 + 1)
            out += bytes(a ^ b for a, b in zip(text[k:k + 16], s))
        return bytes(out)

    if decrypt:
        data = xor_stream(ct)
    flags = (64 if aad else 0) | (((tag_len - 2) // 2) << 3) | (L - 1)
    b = bytes([flags]) + nonce + len(data).to_bytes(L, "big")
    if aad:
        if len(aad) < 0xFF00:
            a = struct.pack(">H", len(aad)) + aad
        elif len(aad) < 1 << 32:
            a = b"\xff\xfe" + struct.pack(">I", len(aad)) + aad
        else:
            a = b"\xff\xff" + struct.pack(">Q", len(aad)) + aad
        b += a + bytes(-len(a) % 16)
    b += data + bytes(-len(data) % 16)
    x = bytes(16)
    for k in range(0, len(b), 16):
        x = block(bytes(p ^ q for p, q in zip(x, b[k:k + 16])))
    t = bytes(p ^ q for p, q in zip(x[:tag_len], ctr(0)))
    if decrypt:
        return data if t == tag else None
    return xor_stream(data) + t


def wycheproof() -> dict:
    d = json.loads(Path(__file__).with_name("aes_ccm_test.json").read_text())
    out = {"vectors": 0, "lib_mismatch": [], "ours_mismatch": [], "skipped_lib": 0}
    for g in d["testGroups"]:
        tag_len = g["tagSize"] // 8
        for t in g["tests"]:
            out["vectors"] += 1
            key, iv, aad = (bytes.fromhex(t[k]) for k in ("key", "iv", "aad"))
            msg, ct, tag = (bytes.fromhex(t[k]) for k in ("msg", "ct", "tag"))
            want_valid = t["result"] == "valid"
            acceptable = t["result"] == "acceptable"
            # ours
            if 7 <= len(iv) <= 13 and tag_len in (4, 6, 8, 10, 12, 14, 16):
                got = ccm(key, iv, aad, ct + tag, tag_len, decrypt=True)
                ok = got == msg
                if not acceptable and ok != want_valid:
                    out["ours_mismatch"].append(t["tcId"])
            # library
            try:
                lib = AESCCM(key, tag_length=tag_len)
                got = lib.decrypt(iv, ct + tag, aad)
                ok = got == msg
            except ValueError:
                out["skipped_lib"] += 1  # parameters the library refuses (invalid by definition)
                ok = False
            except Exception:  # noqa: BLE001 - InvalidTag
                ok = False
            if not acceptable and ok != want_valid:
                out["lib_mismatch"].append(t["tcId"])
    return out


def differential(n: int, seed: int = 1) -> dict:
    rng = random.Random(seed)
    bad = 0
    for _ in range(n):
        key = rng.randbytes(16)
        nonce = rng.randbytes(13)
        aad = rng.randbytes(rng.choice((6, 9, 12)))
        msg = rng.randbytes(rng.randrange(0, 64))
        if ccm(key, nonce, aad, msg, 4) != AESCCM(key, tag_length=4).encrypt(nonce, msg, aad):
            bad += 1
    return {"cases": n, "mismatch": bad}


def layout(n: int, seed: int = 2) -> dict:
    rng = random.Random(seed)
    key = rng.randbytes(16)
    aead = AESCCM(key, tag_length=4)
    bad = 0
    for _ in range(n):
        plain = bytes([rng.choice((1, 2, 3, 4, 5, 7, 0x12)), rng.randrange(256), rng.randrange(256), rng.randrange(256)]) + rng.randbytes(rng.randrange(0, 20))
        fcnt, tx = rng.randrange(1 << 32), rng.randrange(256)
        sealed = S.seal(aead, plain, fcnt, tx)
        header = bytes([plain[0] | 0x80]) + plain[1:4] + struct.pack("<I", fcnt) + bytes([tx])
        nonce = bytes([plain[1], plain[2]]) + struct.pack("<I", fcnt) + bytes([plain[0] | 0x80, tx]) + bytes(5)
        if sealed != header + ccm(key, nonce, header, plain[4:], 4):
            bad += 1
    return {"cases": n, "mismatch": bad}


def run(n: int = 20000) -> dict:
    return {"wycheproof": wycheproof(), "differential": differential(n), "layout": layout(n)}


if __name__ == "__main__":
    print(json.dumps(run(int(sys.argv[1]) if len(sys.argv) > 1 else 20000), indent=1))
