"""SRP6 as used by AzerothCore / the 3.3.5a client. All big numbers are little-endian on the wire/DB."""
import hashlib
import os

N = int("894B645E89E1535BBDAD5B8B290650530801B18EBFBF5E8FAB3C82872A3E9BB7", 16)
g = 7


def sha1(*parts: bytes) -> bytes:
    h = hashlib.sha1()
    for p in parts:
        h.update(p)
    return h.digest()


def le_int(b: bytes) -> int:
    return int.from_bytes(b, "little")


def int_le(i: int, n: int = 32) -> bytes:
    return i.to_bytes(n, "little")


def make_verifier(username: str, password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    """Return (salt, verifier) as 32-byte little-endian blobs, like AccountMgr::CreateAccount."""
    salt = salt or os.urandom(32)
    x = le_int(sha1(salt, sha1(f"{username.upper()}:{password.upper()}".encode())))
    return salt, int_le(pow(g, x, N))
