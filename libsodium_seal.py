"""Pure-stdlib implementation of libsodium `crypto_box_seal`.

GitHub Actions Secrets require the value to be encrypted with libsodium
sealed-box (X25519 + XSalsa20-Poly1305). PyNaCl is often unavailable and we
must stay zero-dependency, so the primitives are implemented here directly.

Implements: X25519, Salsa20/HSalsa20, Poly1305, secretbox, sealed box.
Reference: RFC 7748, NaCl secretbox, libsodium crypto_box_seal.
"""

import hashlib
import os

# ---------------------------------------------------------------- X25519

_P = 2 ** 255 - 19
_A24 = 121665
_BASE_POINT = (9).to_bytes(32, "little")


def _clamp(k: bytes) -> int:
    b = bytearray(k)
    b[0] &= 248
    b[31] &= 127
    b[31] |= 64
    return int.from_bytes(bytes(b), "little")


def x25519(scalar: bytes, u_bytes: bytes) -> bytes:
    """X25519 scalar multiplication (RFC 7748, Montgomery ladder)."""
    k = _clamp(scalar)
    x1 = int.from_bytes(u_bytes, "little") & ((1 << 255) - 1)
    x2, z2 = 1, 0
    x3, z3 = x1, 1
    swap = 0
    for t in range(254, -1, -1):
        kt = (k >> t) & 1
        swap ^= kt
        if swap:
            x2, x3 = x3, x2
            z2, z3 = z3, z2
        swap = kt

        a = (x2 + z2) % _P
        aa = a * a % _P
        b = (x2 - z2) % _P
        bb = b * b % _P
        e = (aa - bb) % _P
        c = (x3 + z3) % _P
        d = (x3 - z3) % _P
        da = d * a % _P
        cb = c * b % _P

        x3 = pow(da + cb, 2, _P)
        z3 = x1 * pow(da - cb, 2, _P) % _P
        x2 = aa * bb % _P
        z2 = e * (aa + _A24 * e) % _P

    if swap:
        x2, x3 = x3, x2
        z2, z3 = z3, z2

    return (x2 * pow(z2, _P - 2, _P) % _P).to_bytes(32, "little")


# ---------------------------------------------------------------- Salsa20

_SIGMA = [0x61707865, 0x3320646E, 0x79622D32, 0x6B206574]


def _rotl(x: int, n: int) -> int:
    return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF


def _qr(x, a, b, c, d):
    x[b] ^= _rotl((x[a] + x[d]) & 0xFFFFFFFF, 7)
    x[c] ^= _rotl((x[b] + x[a]) & 0xFFFFFFFF, 9)
    x[d] ^= _rotl((x[c] + x[b]) & 0xFFFFFFFF, 13)
    x[a] ^= _rotl((x[d] + x[c]) & 0xFFFFFFFF, 18)


def _salsa20_rounds(x):
    for _ in range(10):
        _qr(x, 0, 4, 8, 12)
        _qr(x, 5, 9, 13, 1)
        _qr(x, 10, 14, 2, 6)
        _qr(x, 15, 3, 7, 11)
        _qr(x, 0, 1, 2, 3)
        _qr(x, 5, 6, 7, 4)
        _qr(x, 10, 11, 8, 9)
        _qr(x, 15, 12, 13, 14)


def _words(b: bytes):
    return [int.from_bytes(b[i:i + 4], "little") for i in range(0, len(b), 4)]


def hsalsa20(key: bytes, nonce16: bytes) -> bytes:
    """HSalsa20: key derivation used by XSalsa20 / crypto_box beforenm."""
    k = _words(key)
    n = _words(nonce16)
    x = [
        _SIGMA[0], k[0], k[1], k[2], k[3],
        _SIGMA[1], n[0], n[1], n[2], n[3],
        _SIGMA[2], k[4], k[5], k[6], k[7],
        _SIGMA[3],
    ]
    _salsa20_rounds(x)
    out = [x[0], x[5], x[10], x[15], x[6], x[7], x[8], x[9]]
    return b"".join(w.to_bytes(4, "little") for w in out)


def salsa20_stream(key: bytes, nonce8: bytes, length: int) -> bytes:
    """Salsa20 keystream with a 64-bit block counter."""
    k = _words(key)
    n = _words(nonce8)
    out = bytearray()
    counter = 0
    while len(out) < length:
        state = [
            _SIGMA[0], k[0], k[1], k[2], k[3],
            _SIGMA[1], n[0], n[1],
            counter & 0xFFFFFFFF, (counter >> 32) & 0xFFFFFFFF,
            _SIGMA[2], k[4], k[5], k[6], k[7],
            _SIGMA[3],
        ]
        x = list(state)
        _salsa20_rounds(x)
        block = [(x[i] + state[i]) & 0xFFFFFFFF for i in range(16)]
        out += b"".join(w.to_bytes(4, "little") for w in block)
        counter += 1
    return bytes(out[:length])


# ---------------------------------------------------------------- Poly1305

def poly1305(msg: bytes, key: bytes) -> bytes:
    r = int.from_bytes(key[:16], "little") & 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF
    s = int.from_bytes(key[16:32], "little")
    p = (1 << 130) - 5
    acc = 0
    for i in range(0, len(msg), 16):
        chunk = msg[i:i + 16]
        n = int.from_bytes(chunk + b"\x01", "little")
        acc = ((acc + n) * r) % p
    return ((acc + s) % (1 << 128)).to_bytes(16, "little")


# ---------------------------------------------------------------- secretbox

def secretbox(msg: bytes, nonce24: bytes, key32: bytes) -> bytes:
    """XSalsa20-Poly1305. Returns tag(16) || ciphertext."""
    subkey = hsalsa20(key32, nonce24[:16])
    stream = salsa20_stream(subkey, nonce24[16:24], 32 + len(msg))
    poly_key = stream[:32]
    ct = bytes(a ^ b for a, b in zip(msg, stream[32:]))
    return poly1305(ct, poly_key) + ct


def box_beforenm(pk: bytes, sk: bytes) -> bytes:
    """crypto_box_beforenm: HSalsa20(X25519(sk, pk), 0^16)."""
    return hsalsa20(x25519(sk, pk), b"\x00" * 16)


# ---------------------------------------------------------------- sealed box

def seal(message: bytes, recipient_pk: bytes) -> bytes:
    """crypto_box_seal: returns epk(32) || secretbox(...)."""
    esk = os.urandom(32)
    epk = x25519(esk, _BASE_POINT)
    nonce = hashlib.blake2b(epk + recipient_pk, digest_size=24).digest()
    return epk + secretbox(message, nonce, box_beforenm(recipient_pk, esk))


def seal_b64(message: bytes, public_key_b64: str) -> str:
    """Encrypt for a GitHub-returned base64 public key, return base64 value."""
    import base64
    pk = base64.b64decode(public_key_b64)
    return base64.b64encode(seal(message, pk)).decode("ascii")


# ---------------------------------------------------------------- self test

def _self_test() -> bool:
    ok = True

    # RFC 7748 section 5.2 vector 1
    scalar = bytes.fromhex(
        "a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4")
    u = bytes.fromhex(
        "e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c")
    want = "c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552"
    got = x25519(scalar, u).hex()
    print(f"[x25519 ] {'PASS' if got == want else 'FAIL'}  {got[:16]}...")
    ok &= got == want

    # RFC 7748 Diffie-Hellman symmetry
    a_sk, b_sk = bytes(range(32)), bytes(range(32, 64))
    a_pk, b_pk = x25519(a_sk, _BASE_POINT), x25519(b_sk, _BASE_POINT)
    ok &= box_beforenm(b_pk, a_sk) == box_beforenm(a_pk, b_sk)
    print(f"[dh     ] {'PASS' if ok else 'FAIL'}  shared secret matches")

    # NaCl secretbox test vector (crypto_secretbox_xsalsa20poly1305)
    key = bytes.fromhex(
        "1b27556473e985d462cd51197a9a46c76009549eac6474f206c4ee0844f68389")
    nonce = bytes.fromhex("69696ee955b62b73cd62bda875fc73d68219e0036b7a0b37")
    msg = bytes.fromhex(
        "be075fc53c81f2d5cf141316ebeb0c7b5228c52a4c62cbd44b66849b64244ffc"
        "e5ecbaaf33bd751a1ac728d45e6c61296cdc3c01233561f41db66cce314adb31"
        "0e3be8250c46f06dceea3a7fa1348057e2f6556ad6b1318a024a838f21af1fde"
        "048977eb48f59ffd4924ca1c60902e52f0a089bc76897040e082f93776384864"
        "5e0705")
    want_tag = bytes.fromhex("f3ffc7703f9400e52a7dfb4b3d3305d9")
    boxed = secretbox(msg, nonce, key)
    got_tag = boxed[:16]
    print(f"[poly1305] {'PASS' if got_tag == want_tag else 'FAIL'}  "
          f"{got_tag.hex()}")
    ok &= got_tag == want_tag

    # sealed box round-trip shape
    pk = x25519(bytes(range(32)), _BASE_POINT)
    sealed = seal(b"hello-secret", pk)
    print(f"[seal   ] {'PASS' if len(sealed) == 32 + 16 + 12 else 'FAIL'}  "
          f"len={len(sealed)}")

    return bool(ok)


if __name__ == "__main__":
    raise SystemExit(0 if _self_test() else 1)
