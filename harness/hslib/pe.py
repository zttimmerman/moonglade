"""Read-only PE (Windows executable) version-resource reader. Stdlib only.

Parses the DOS/PE headers, maps the resource data directory to its section, walks the
resource tree to RT_VERSION, and decodes VS_VERSIONINFO: the binary VS_FIXEDFILEINFO and
the StringFileInfo string table. Opens the file read-only and never writes anything.
"""
from __future__ import annotations

import io
import struct
from pathlib import Path

RT_VERSION = 16
VS_FFI_SIGNATURE = 0xFEEF04BD


class PEError(Exception):
    pass


def _u16(b, o): return struct.unpack_from("<H", b, o)[0]
def _u32(b, o): return struct.unpack_from("<I", b, o)[0]


def _read_at(f, off: int, n: int) -> bytes:
    f.seek(off)
    data = f.read(n)
    if len(data) != n:
        raise PEError(f"truncated read at 0x{off:x} (+{n})")
    return data


def _sections(f):
    dos = _read_at(f, 0, 64)
    if dos[:2] != b"MZ":
        raise PEError("no MZ header")
    pe_off = _u32(dos, 0x3C)
    hdr = _read_at(f, pe_off, 24)
    if hdr[:4] != b"PE\0\0":
        raise PEError("no PE signature")
    machine, nsec = _u16(hdr, 4), _u16(hdr, 6)
    opt_size = _u16(hdr, 20)
    opt = _read_at(f, pe_off + 24, opt_size)
    magic = _u16(opt, 0)
    if magic == 0x10B:
        dd_off, kind = 96, "PE32"
    elif magic == 0x20B:
        dd_off, kind = 112, "PE32+"
    else:
        raise PEError(f"unknown optional header magic 0x{magic:x}")
    n_dd = _u32(opt, dd_off - 4)
    if n_dd <= 2:
        raise PEError("no resource data directory")
    rsrc_rva, rsrc_size = struct.unpack_from("<II", opt, dd_off + 2 * 8)
    sec_tbl = _read_at(f, pe_off + 24 + opt_size, 40 * nsec)
    secs = []
    for i in range(nsec):
        name, vsize, va, rawsize, rawptr = struct.unpack_from("<8sIIII", sec_tbl, 40 * i)
        secs.append({"name": name.rstrip(b"\0").decode("latin-1"), "va": va,
                     "vsize": max(vsize, rawsize), "rawptr": rawptr, "rawsize": rawsize})
    return {"machine": machine, "kind": kind, "rsrc_rva": rsrc_rva, "rsrc_size": rsrc_size,
            "sections": secs}


def _rva_to_off(secs, rva: int) -> int:
    for s in secs:
        if s["va"] <= rva < s["va"] + s["vsize"]:
            return s["rawptr"] + (rva - s["va"])
    raise PEError(f"RVA 0x{rva:x} not in any section")


def _first_entry(rsrc: bytes, dir_off: int, want_id: int | None = None):
    """Return (is_dir, offset) of the matching (or first) entry of a resource directory."""
    n_named, n_id = _u16(rsrc, dir_off + 12), _u16(rsrc, dir_off + 14)
    for i in range(n_named + n_id):
        e = dir_off + 16 + 8 * i
        name, target = _u32(rsrc, e), _u32(rsrc, e + 4)
        is_id = not (name & 0x80000000)
        if want_id is None or (is_id and name == want_id):
            return bool(target & 0x80000000), target & 0x7FFFFFFF
    return None


def _wstr(b: bytes, o: int) -> tuple[str, int]:
    """UTF-16LE NUL-terminated string at o → (text, offset after NUL)."""
    end = o
    while end + 1 < len(b) and b[end:end + 2] != b"\0\0":
        end += 2
    return b[o:end].decode("utf-16-le", errors="replace"), end + 2


def _align4(o: int) -> int:
    return (o + 3) & ~3


def _parse_block(b: bytes, o: int):
    """One VS_VERSIONINFO-style block → (key, value_bytes, wType, children_range, end)."""
    length, vlen, wtype = struct.unpack_from("<HHH", b, o)
    end = o + length
    key, p = _wstr(b, o + 6)
    p = _align4(p)
    vbytes = vlen * 2 if wtype == 1 else vlen
    value = b[p:p + vbytes]
    p = _align4(p + vbytes)
    return key, value, wtype, (p, end), _align4(end)


def _children(b: bytes, rng):
    p, end = rng
    while p + 6 <= end:
        length = _u16(b, p)
        if length == 0:
            break
        yield _parse_block(b, p)
        p = _align4(p + length)


def version_info(path: Path | bytes) -> dict:
    """Return the file's version resource: fixed file/product versions + string table. `path` may also be the file's
    bytes (easy-setup: bootstrap reads Wow.exe straight out of a client zip, writing nothing)."""
    if isinstance(path, (bytes, bytearray)):
        fobj, path = io.BytesIO(bytes(path)), "<bytes>"
    else:
        path = Path(path)
        fobj = path.open("rb")
    with fobj as f:
        hdr = _sections(f)
        if not hdr["rsrc_rva"]:
            raise PEError("PE has no resource section")
        rsrc_off = _rva_to_off(hdr["sections"], hdr["rsrc_rva"])
        rsrc = _read_at(f, rsrc_off, hdr["rsrc_size"])
        lvl = _first_entry(rsrc, 0, RT_VERSION)
        if not lvl or not lvl[0]:
            raise PEError("no RT_VERSION resource")
        lvl = _first_entry(rsrc, lvl[1])          # name (usually ID 1)
        if not lvl or not lvl[0]:
            raise PEError("malformed RT_VERSION name directory")
        lvl = _first_entry(rsrc, lvl[1])          # language
        if not lvl or lvl[0]:
            raise PEError("malformed RT_VERSION language entry")
        data_rva, data_size = struct.unpack_from("<II", rsrc, lvl[1])
        vi = _read_at(f, _rva_to_off(hdr["sections"], data_rva), data_size)

    key, value, _, kids, _ = _parse_block(vi, 0)
    if key != "VS_VERSION_INFO":
        raise PEError(f"unexpected version block key {key!r}")
    if len(value) < 52 or _u32(value, 0) != VS_FFI_SIGNATURE:
        raise PEError("VS_FIXEDFILEINFO missing or bad signature")
    fms, fls, pms, pls = struct.unpack_from("<IIII", value, 8)
    fv = (fms >> 16, fms & 0xFFFF, fls >> 16, fls & 0xFFFF)
    pv = (pms >> 16, pms & 0xFFFF, pls >> 16, pls & 0xFFFF)
    strings: dict[str, str] = {}
    for ckey, _cv, _ct, crng, _ in _children(vi, kids):
        if ckey != "StringFileInfo":
            continue
        for _tkey, _tv, _tt, trng, _ in _children(vi, crng):   # per-language string tables
            for skey, sval, stype, _r, _ in _children(vi, trng):
                strings[skey] = sval.decode("utf-16-le", errors="replace").rstrip("\0") if stype == 1 \
                    else sval.decode("latin-1").rstrip("\0")
    return {
        "path": str(path),
        "pe_kind": hdr["kind"],
        "machine": f"0x{hdr['machine']:04x}",
        "file_version": ".".join(map(str, fv)),
        "product_version": ".".join(map(str, pv)),
        "strings": strings,
    }
