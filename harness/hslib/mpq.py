"""MPQ (MoPaQ) reader, stdlib only: `hs mpq list|extract` (login-screen spike, A.1), plus a minimal writer
(`write_archive`, `hs mpq write`, spike B.1) for throwaway test patch archives.

Existing archives are only ever opened with mode "rb"; extraction and writing refuse any destination under client/.

Format sources (no code copied; written from the descriptions):
- Justin Olbrantz (Quantam), "The MoPaQ Archive Format" (inside_mopaq), and Ladislav Zezula's StormLib format
  notes, http://www.zezula.net/en/mpq/mpqformat.html: header v1/v2, hash/block tables, hi-block table,
  the crypt table, HashString, table/file encryption and the FIX_KEY rule, sector offset tables, compression masks.
- wowdev.wiki "MPQ" page (same layout, WoW-specific notes: 3.3.5a archives are format v1/v2).
- PKWARE DCL "implode" decompression follows the bit format documented in Mark Adler's blast.c (zlib contrib/blast):
  LSB-first bit stream, inverted Huffman codes, the literal/length/distance code-length tables and base/extra tables.
Supported: zlib (0x02), bzip2 (0x10), PKWARE implode (0x08 mask, or the IMPLODE block flag), stored, single-unit,
encrypted (incl. FIX_KEY), sector CRC tables (skipped, not validated). Not needed for 3.3.5a GlueXML/models/music and
not supported: Huffman (0x01) / ADPCM (0x40/0x80, WAVE only), LZMA (0x12), sparse (0x20), patch files (Cataclysm+).
"""
import bz2
import os
import struct
import zlib
from pathlib import Path

from . import paths as P


class MPQError(RuntimeError):
    pass


# ---- crypto -------------------------------------------------------------------------------------------------------
def _crypt_table() -> list[int]:
    table = [0] * 0x500
    seed = 0x00100001
    for i in range(256):
        idx = i
        for _ in range(5):
            seed = (seed * 125 + 3) % 0x2AAAAB
            hi = (seed & 0xFFFF) << 16
            seed = (seed * 125 + 3) % 0x2AAAAB
            table[idx] = hi | (seed & 0xFFFF)
            idx += 256
    return table


CRYPT = _crypt_table()
M32 = 0xFFFFFFFF
HASH_OFFSET, HASH_A, HASH_B, HASH_KEY = 0, 1, 2, 3


def hash_string(s: str, kind: int) -> int:
    seed1, seed2 = 0x7FED7FED, 0xEEEEEEEE
    for ch in s.upper().encode("latin-1"):
        seed1 = (CRYPT[kind * 256 + ch] ^ (seed1 + seed2)) & M32
        seed2 = (ch + seed1 + seed2 + (seed2 << 5) + 3) & M32
    return seed1


def encrypt(data: bytes, key: int) -> bytes:
    """Inverse of decrypt (same key schedule; seed2 advances with the plaintext word)."""
    n = len(data) // 4
    words = struct.unpack(f"<{n}I", data[:n * 4])
    out = [0] * n
    seed2 = 0xEEEEEEEE
    for i, v in enumerate(words):
        seed2 = (seed2 + CRYPT[0x400 + (key & 0xFF)]) & M32
        out[i] = (v ^ (key + seed2)) & M32
        key = (((~key << 0x15) + 0x11111111) & M32) | (key >> 0x0B)
        seed2 = (v + seed2 + (seed2 << 5) + 3) & M32
    return struct.pack(f"<{n}I", *out) + data[n * 4:]


def decrypt(data: bytes, key: int) -> bytes:
    n = len(data) // 4
    words = struct.unpack(f"<{n}I", data[:n * 4])
    out = [0] * n
    seed2 = 0xEEEEEEEE
    for i, w in enumerate(words):
        seed2 = (seed2 + CRYPT[0x400 + (key & 0xFF)]) & M32
        v = (w ^ (key + seed2)) & M32
        out[i] = v
        key = (((~key << 0x15) + 0x11111111) & M32) | (key >> 0x0B)
        seed2 = (v + seed2 + (seed2 << 5) + 3) & M32
    return struct.pack(f"<{n}I", *out) + data[n * 4:]


# ---- PKWARE DCL explode (bit format per blast.c) ------------------------------------------------------------------
_LITLEN = bytes([11, 124, 8, 7, 28, 7, 188, 13, 76, 4, 10, 8, 12, 10, 12, 10, 8, 23, 8, 9, 7, 6, 7, 8, 7, 6, 55, 8,
                 23, 24, 12, 11, 7, 9, 11, 12, 6, 7, 22, 5, 7, 24, 6, 11, 9, 6, 7, 22, 7, 11, 38, 7, 9, 8, 25, 11, 8,
                 11, 9, 12, 8, 12, 5, 38, 5, 38, 5, 11, 7, 5, 6, 21, 6, 10, 53, 8, 7, 24, 10, 27, 44, 253, 253, 253,
                 252, 252, 252, 13, 12, 45, 12, 45, 12, 61, 12, 45, 44, 173])
_LENLEN = bytes([2, 35, 36, 53, 38, 23])
_DISTLEN = bytes([2, 20, 53, 230, 247, 151, 248])
_BASE = (3, 2, 4, 5, 6, 7, 8, 9, 10, 12, 16, 24, 40, 72, 136, 264)
_EXTRA = (0, 0, 0, 0, 0, 0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8)


def _huffman(rep: bytes, nsym: int):
    lengths = []
    for b in rep:
        lengths += [b & 15] * ((b >> 4) + 1)
    if len(lengths) != nsym:
        raise MPQError(f"implode table expands to {len(lengths)} symbols, expected {nsym}")
    count = [0] * 14
    for ln in lengths:
        count[ln] += 1
    left = 1
    for ln in range(1, 14):
        left = (left << 1) - count[ln]
    if left != 0:
        raise MPQError("implode Huffman table is not a complete code")
    offs = [0] * 14
    for ln in range(1, 13):
        offs[ln + 1] = offs[ln] + count[ln]
    symbol = [0] * nsym
    for s, ln in enumerate(lengths):
        if ln:
            symbol[offs[ln]] = s
            offs[ln] += 1
    return count, symbol


_LITCODE, _LENCODE, _DISTCODE = _huffman(_LITLEN, 256), _huffman(_LENLEN, 16), _huffman(_DISTLEN, 64)


def explode(src: bytes, out_size: int) -> bytes:
    if len(src) < 2:
        raise MPQError("implode stream too short")
    lit, dic = src[0], src[1]
    if lit > 1 or dic not in (4, 5, 6):
        raise MPQError(f"bad implode header lit={lit} dict={dic}")
    pos, buf, cnt = 2, 0, 0
    out = bytearray()

    def bits(n):
        nonlocal pos, buf, cnt
        while cnt < n:
            if pos >= len(src):
                raise MPQError("implode stream ran out of input")
            buf |= src[pos] << cnt
            pos += 1
            cnt += 8
        v = buf & ((1 << n) - 1)
        buf >>= n
        cnt -= n
        return v

    def decode(h):
        count, symbol = h
        code = first = index = 0
        for ln in range(1, 14):
            code |= bits(1) ^ 1
            c = count[ln]
            if code < first + c:
                return symbol[index + code - first]
            index += c
            first = (first + c) << 1
            code <<= 1
        raise MPQError("bad implode code")

    while True:
        if bits(1):
            sym = decode(_LENCODE)
            length = _BASE[sym] + bits(_EXTRA[sym])
            if length == 519:
                break
            sh = 2 if length == 2 else dic
            dist = (decode(_DISTCODE) << sh) + bits(sh) + 1
            if dist > len(out):
                raise MPQError("implode distance too far back")
            for _ in range(length):
                out.append(out[-dist])
        else:
            out.append(decode(_LITCODE) if lit else bits(8))
        if len(out) >= out_size:
            break
    return bytes(out[:out_size])


# ---- archive ------------------------------------------------------------------------------------------------------
F_IMPLODE, F_COMPRESS, F_ENCRYPTED, F_FIX_KEY = 0x100, 0x200, 0x10000, 0x20000
F_PATCH, F_SINGLE_UNIT, F_DELETE, F_SECTOR_CRC, F_EXISTS = 0x100000, 0x1000000, 0x2000000, 0x4000000, 0x80000000


class Archive:
    def __init__(self, path):
        self.path = Path(path)
        self.fh = open(self.path, "rb")          # read-only, always
        self._read_header()
        self._read_tables()

    def close(self):
        self.fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _read_header(self):
        size = self.path.stat().st_size
        off = 0
        while off < size:
            self.fh.seek(off)
            magic = self.fh.read(4)
            if magic == b"MPQ\x1a":
                break
            if magic == b"MPQ\x1b":                  # user-data header points at the real one
                _, hdr_off = struct.unpack("<II", self.fh.read(8))
                off += hdr_off
                continue
            off += 512
        else:
            raise MPQError(f"{self.path}: no MPQ header")
        self.base = off
        h = self.fh.read(0x2C - 4)
        (self.header_size, self.archive_size, self.version, shift, ht_pos, bt_pos,
         self.ht_size, self.bt_size) = struct.unpack("<IIHHIIII", h[:28])
        if self.version > 1:
            raise MPQError(f"{self.path}: MPQ format v{self.version + 1} not supported (3.3.5a uses v1/v2)")
        self.sector_size = 512 << shift
        hi_bt = ht_hi = bt_hi = 0
        if self.version == 1:
            hi_bt, ht_hi, bt_hi = struct.unpack("<QHH", h[28:40])
        self.ht_pos = ht_pos | (ht_hi << 32)
        self.bt_pos = bt_pos | (bt_hi << 32)
        self.hi_bt_pos = hi_bt

    def _read_tables(self):
        self.fh.seek(self.base + self.ht_pos)
        raw = decrypt(self.fh.read(self.ht_size * 16), hash_string("(hash table)", HASH_KEY))
        self.hashes = [struct.unpack_from("<IIHHI", raw, i * 16) for i in range(self.ht_size)]
        self.fh.seek(self.base + self.bt_pos)
        raw = decrypt(self.fh.read(self.bt_size * 16), hash_string("(block table)", HASH_KEY))
        self.blocks = [list(struct.unpack_from("<IIII", raw, i * 16)) for i in range(self.bt_size)]
        if self.hi_bt_pos:
            self.fh.seek(self.base + self.hi_bt_pos)
            hi = struct.unpack(f"<{self.bt_size}H", self.fh.read(self.bt_size * 2))
            for b, h in zip(self.blocks, hi):
                b[0] |= h << 32

    def find(self, name: str):
        """Block index for `name` (neutral locale preferred), or None."""
        name = name.replace("/", "\\")
        start = hash_string(name, HASH_OFFSET) % self.ht_size
        a, b = hash_string(name, HASH_A), hash_string(name, HASH_B)
        hits = []
        for i in range(self.ht_size):
            na, nb, locale, _plat, bi = self.hashes[(start + i) % self.ht_size]
            if bi == 0xFFFFFFFF:
                break
            if na == a and nb == b and bi < self.bt_size:
                hits.append((locale != 0, bi))
        for _, bi in sorted(hits):
            flags = self.blocks[bi][3]
            if flags & F_EXISTS and not flags & F_DELETE:
                return bi
        return None

    def entries_in_use(self) -> int:
        return sum(1 for h in self.hashes if h[4] < self.bt_size)

    def names(self) -> list[str]:
        """Names from (listfile) that resolve to an existing block in this archive."""
        bi = self.find("(listfile)")
        if bi is None:
            raise MPQError(f"{self.path}: no (listfile); names unknown")
        text = self._read_block(bi, "(listfile)").decode("latin-1")
        seen, out = set(), []
        for n in text.replace(";", "\n").replace("\r", "\n").split("\n"):
            n = n.strip()
            if n and n.lower() not in seen and self.find(n) is not None:
                seen.add(n.lower())
                out.append(n)
        return out

    def info(self, name: str) -> dict:
        bi = self.find(name)
        if bi is None:
            raise MPQError(f"{name!r} not in {self.path.name}")
        off, csize, fsize, flags = self.blocks[bi]
        return {"name": name, "block": bi, "offset": off, "csize": csize, "size": fsize, "flags": hex(flags)}

    def read(self, name: str) -> bytes:
        bi = self.find(name)
        if bi is None:
            raise MPQError(f"{name!r} not in {self.path.name}")
        return self._read_block(bi, name)

    def _decompress(self, data: bytes, size: int, flags: int) -> bytes:
        if flags & F_IMPLODE:
            return explode(data, size)
        mask, data = data[0], data[1:]
        if mask & ~(0x02 | 0x08 | 0x10):
            raise MPQError(f"unsupported compression mask {mask:#x}")
        if mask & 0x10:
            data = bz2.decompress(data)
        if mask & 0x08:
            data = explode(data, size)
        if mask & 0x02:
            data = zlib.decompress(data)
        return data

    def _read_block(self, bi: int, name: str) -> bytes:
        off, csize, fsize, flags = self.blocks[bi]
        if flags & F_PATCH:
            raise MPQError(f"{name}: patch-file entries not supported")
        key = 0
        if flags & F_ENCRYPTED:
            key = hash_string(name.replace("/", "\\").rsplit("\\", 1)[-1], HASH_KEY)
            if flags & F_FIX_KEY:
                key = ((key + off) ^ fsize) & M32
        self.fh.seek(self.base + off)
        raw = self.fh.read(csize)
        compressed = flags & (F_COMPRESS | F_IMPLODE)
        if flags & F_SINGLE_UNIT:
            if flags & F_ENCRYPTED:
                raw = decrypt(raw, key)
            return self._decompress(raw, fsize, flags) if compressed and csize < fsize else raw[:fsize]
        ssize = self.sector_size
        nsec = (fsize + ssize - 1) // ssize
        if compressed:
            first = struct.unpack("<I", decrypt(raw[:4], (key - 1) & M32) if flags & F_ENCRYPTED else raw[:4])[0]
            # The table's first entry is its own length: nsec+1 entries, +1 with SECTOR_CRC (some writers add the
            # extra entry even without the flag). Some installer-built archives (backup-enUS.MPQ: header says 16 KiB)
            # store files in another sector size; the table length tells which one.
            def fits(s):
                k = (fsize + s - 1) // s
                return first == (k + 2) * 4 if flags & F_SECTOR_CRC else first in ((k + 1) * 4, (k + 2) * 4)
            if not fits(ssize):
                for s in (512 << shift for shift in range(16)):
                    if fits(s):
                        ssize, nsec = s, (fsize + s - 1) // s
                        break
                else:
                    raise MPQError(f"{name}: sector offset table inconsistent with file size")
            n = first // 4
            tbl = raw[:n * 4]
            if flags & F_ENCRYPTED:
                tbl = decrypt(tbl, (key - 1) & M32)
            offsets = struct.unpack(f"<{n}I", tbl)
        else:
            offsets = [min(i * ssize, csize) for i in range(nsec + 1)]
        out = bytearray()
        for i in range(nsec):
            sec = raw[offsets[i]:offsets[i + 1]]
            if flags & F_ENCRYPTED:
                sec = decrypt(sec, (key + i) & M32)
            want = min(ssize, fsize - i * ssize)
            out += self._decompress(sec, want, flags) if compressed and len(sec) < want else sec[:want]
        if len(out) != fsize:
            raise MPQError(f"{name}: got {len(out)} bytes, block table says {fsize}")
        return bytes(out)


# ---- writer -------------------------------------------------------------------------------------------------------
def write_archive(dest, files: dict, compress: bool = True, sector_shift: int = 3, client_root=None) -> dict:
    """Write a new format-v1 MPQ at `dest` holding `files` ({internal name: bytes}) plus a (listfile).

    Minimal by design (test patch archives only): neutral locale, no (attributes), no per-file encryption,
    no single-unit files. Each file is stored in sectors of 512 << sector_shift bytes; with `compress`, a sector is
    zlib-compressed (mask byte 0x02) when that makes it smaller, else stored raw, behind a sector offset table.
    Hash and block tables are encrypted with the standard keys. Refuses destinations under client/ (`client_root`,
    default paths.CLIENT; a check passes a scratch stand-in so the refusal is never tested against the real client)."""
    dest = Path(dest)
    from .pristine import protected_root_of
    if _under_client(dest, client_root) or _under_client(dest.parent, client_root) or protected_root_of(dest):
        raise MPQError(f"refusing to write under client/ (the pristine client): {dest}")
    norm = {}
    for name, data in files.items():
        name = name.replace("/", "\\")
        if name.lower() in {n.lower() for n in norm} or name.lower() == "(listfile)":
            raise MPQError(f"duplicate or reserved name {name!r}")
        norm[name] = bytes(data)
    norm["(listfile)"] = "\r\n".join(n for n in norm).encode("latin-1") + b"\r\n"
    ssize = 512 << sector_shift
    body, blocks = bytearray(), []
    hdr_size = 0x20
    for name, data in norm.items():
        off = hdr_size + len(body)
        if not data:
            blocks.append((off, 0, 0, F_EXISTS))
            continue
        if compress:
            secs = []
            for i in range(0, len(data), ssize):
                raw = data[i:i + ssize]
                z = b"\x02" + zlib.compress(raw, 9)
                secs.append(z if len(z) < len(raw) else raw)
            table, pos = [], (len(secs) + 1) * 4
            for sec in secs:
                table.append(pos)
                pos += len(sec)
            table.append(pos)
            blob = struct.pack(f"<{len(table)}I", *table) + b"".join(secs)
            blocks.append((off, len(blob), len(data), F_EXISTS | F_COMPRESS))
        else:
            blob = data
            blocks.append((off, len(data), len(data), F_EXISTS))
        body += blob
    ht_size = 16
    while ht_size < 2 * len(norm):
        ht_size *= 2
    ht = [(M32, M32, 0xFFFF, 0xFFFF, M32)] * ht_size
    for bi, name in enumerate(norm):
        i = hash_string(name, HASH_OFFSET) % ht_size
        while ht[i][4] != M32:
            i = (i + 1) % ht_size
        ht[i] = (hash_string(name, HASH_A), hash_string(name, HASH_B), 0, 0, bi)
    ht_raw = b"".join(struct.pack("<IIHHI", *e) for e in ht)
    bt_raw = b"".join(struct.pack("<IIII", *b) for b in blocks)
    ht_pos = hdr_size + len(body)
    bt_pos = ht_pos + len(ht_raw)
    total = bt_pos + len(bt_raw)
    header = struct.pack("<4sIIHHIIII", b"MPQ\x1a", hdr_size, total, 0, sector_shift, ht_pos, bt_pos,
                         ht_size, len(blocks))
    out = header + bytes(body) + encrypt(ht_raw, hash_string("(hash table)", HASH_KEY)) \
        + encrypt(bt_raw, hash_string("(block table)", HASH_KEY))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(out)
    return {"archive": str(dest), "bytes": len(out), "files": [n for n in norm if n != "(listfile)"],
            "hash_table_size": ht_size, "compress": compress}


def verify_written(dest, files: dict) -> dict:
    """Read `dest` back with the reader and require every file byte-identical and listed."""
    with Archive(dest) as a:
        names = a.names()
        bad = [n for n, d in files.items() if a.read(n) != bytes(d)]
    missing = sorted({n.replace("/", "\\").lower() for n in files} - {n.lower() for n in names})
    if bad or missing:
        raise MPQError(f"read-back mismatch: differing={bad} missing_from_listfile={missing}")
    return {"read_back_ok": True, "listed": names}


# ---- CLI helpers --------------------------------------------------------------------------------------------------
def resolve_archive(arg) -> Path:
    p = Path(arg)
    if not p.exists() and (P.CLIENT / p).exists():
        p = P.CLIENT / p
    if not p.is_file():
        raise MPQError(f"no archive at {arg} (also tried client/{arg})")
    return p


def _under_client(p: Path, client_root=None) -> bool:
    """True if `p` (existing or not) would land inside the client dir (`client_root`, default paths.CLIENT), by
    file-system identity. Moved to pristine.inside (easy-setup: every play-client writer shares it); kept as a name."""
    from .pristine import inside
    return inside(p, client_root)


def list_archive(arg, pattern: str | None = None) -> list[str]:
    with Archive(resolve_archive(arg)) as a:
        names = a.names()
    if pattern:
        pat = pattern.lower().replace("/", "\\")
        names = [n for n in names if pat in n.lower()]
    return names


def extract(arg, inside: str, dest, client_root=None) -> dict:
    dest = Path(dest)
    if dest.is_dir() or str(dest).endswith(("/", os.sep)):
        dest = dest / inside.replace("/", "\\").rsplit("\\", 1)[-1]
    if _under_client(dest, client_root) or _under_client(dest.parent, client_root):
        raise MPQError(f"refusing to write under client/: {dest}")
    with Archive(resolve_archive(arg)) as a:
        info = a.info(inside)
        data = a.read(inside)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return {**info, "archive": str(resolve_archive(arg)), "dest": str(dest), "written": len(data)}
