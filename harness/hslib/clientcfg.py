"""Point a WotLK 3.3.5a (build 12340) client directory at the local server.

Config only: writes `Data/<locale>/realmlist.wtf` and edits `WTF/Config.wtf`.
It never writes, renames, or otherwise touches a client binary or archive
(.exe/.dll/.MPQ); see CLAUDE.md section 2 ("No client binary patching").
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
from pathlib import Path

LOCALHOST = "127.0.0.1"
BACKUP_SUFFIX = ".hs-bak"
FORBIDDEN_SUFFIXES = {".exe", ".dll", ".mpq"}
LOCALE_RE = re.compile(r"^[a-z]{2}[A-Z]{2}$")          # enUS, enGB, deDE, ...
CONFIG_KEYS = {"realmlist": LOCALHOST, "patchlist": LOCALHOST}
# Wine-stability defaults (ADR-0004), used by `hs launch`. Unlike CONFIG_KEYS they're only
# added when the key is absent, so a setting Zach changes in-game later is never overridden.
# gxApi d3d9 (via Wine's wined3d). The client's native OpenGL path is unusable on this Wine: winemac offers no
# WGL pixel format matching its request ("SetupPixelFormatWGL(): pixelFormat does not match requested" →
# "no output device available", 2026-10-01). wined3d's render bug (F3) is being fixed in the prefix, not by
# switching renderers. Added only when absent.
WINE_DEFAULTS = {"gxWindow": "1", "gxApi": "d3d9"}


class ClientConfigError(Exception):
    """The directory isn't a usable 3.3.5a client, or a write was refused."""


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _child_ci(parent: Path, name: str) -> Path | None:
    """Case-insensitive lookup of a direct child (Windows trees vary in case)."""
    exact = parent / name
    if exact.exists():
        return exact
    for c in parent.iterdir():
        if c.name.lower() == name.lower():
            return c
    return None


def _guard(target: Path) -> None:
    """Refuse any write whose path (or symlink target) is a binary/archive. Inside the pristine client (stock mode
    configures client/ itself) only the settings file under WTF/ and realmlist.wtf may be written (pristine.py); the
    rest of client/ is also write-protected by the OS."""
    for p in (target, target.resolve()):
        if p.suffix.lower() in FORBIDDEN_SUFFIXES:
            raise ClientConfigError(f"refusing to write client binary/archive: {p}")
    from .pristine import protected_root_of
    root = protected_root_of(target)
    if root is not None:
        rel = Path(os.path.relpath(os.path.realpath(target), os.path.realpath(root)))
        name = rel.name.lower()
        if not ((rel.parts[0].upper() == "WTF" and name.startswith("config.wtf"))
                or (name.startswith("realmlist.wtf") and len(rel.parts) == 3 and rel.parts[0].lower() == "data")):
            raise ClientConfigError(f"refusing to write {target}: inside the pristine client {root} only "
                                    "WTF/Config.wtf and Data/<locale>/realmlist.wtf are configuration")


def _eol_of(data: bytes) -> bytes:
    """Dominant line ending of existing content; CRLF for new files (Windows client)."""
    if not data:
        return b"\r\n"
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n") - crlf
    return b"\r\n" if crlf >= lf and crlf > 0 else b"\n"


def _backup_once(p: Path) -> str | None:
    """Copy p to p.hs-bak if p exists and no backup exists yet."""
    if not p.exists():
        return None
    bak = p.with_name(p.name + BACKUP_SUFFIX)
    _guard(bak)
    if bak.exists():
        return None
    shutil.copy2(p, bak)
    return str(bak)


def _write_if_changed(p: Path, new: bytes) -> dict:
    """Write atomically only when content differs. Returns a change record."""
    _guard(p)
    old = p.read_bytes() if p.exists() else None
    rec = {"path": str(p), "existed": old is not None, "changed": old != new}
    if old == new:
        rec["backup"] = None
        return rec
    rec["backup"] = _backup_once(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".hs-tmp")
    _guard(tmp)
    tmp.write_bytes(new)
    os.replace(tmp, p)
    return rec


def _realmlist_bytes(existing: bytes | None) -> bytes:
    return f"set realmlist {LOCALHOST}".encode() + _eol_of(existing or b"")


def _update_config(data: bytes, keys: dict[str, str]) -> tuple[bytes, dict]:
    """Set `SET <key> "<value>"` for each key, case-insensitively replacing existing
    lines in place; every other line (and its order and ending) is kept byte-for-byte.
    Missing keys are appended using the file's dominant line ending."""
    eol = _eol_of(data)
    lines = data.splitlines(keepends=True)
    want = {k.lower(): (k, v) for k, v in keys.items()}
    seen: dict[str, list] = {}
    out = []
    for line in lines:
        body = line.rstrip(b"\r\n")
        ending = line[len(body):]
        m = re.match(rb"^\s*SET\s+(\S+)", body, re.IGNORECASE)
        k = m.group(1).decode("latin-1").lower() if m else None
        if k in want:
            name, val = want[k]
            new = f'SET {name} "{val}"'.encode()
            seen.setdefault(name, []).append(body.decode("latin-1"))
            out.append(new + (ending or eol))
        else:
            out.append(line)
    if out and not out[-1].endswith((b"\n", b"\r")):
        out[-1] += eol
    appended = []
    for name, val in keys.items():
        if name not in seen:
            out.append(f'SET {name} "{val}"'.encode() + eol)
            appended.append(name)
    return b"".join(out), {"replaced": seen, "appended": appended,
                           "line_ending": "CRLF" if eol == b"\r\n" else "LF"}


def _add_defaults(data: bytes, defaults: dict[str, str]) -> tuple[bytes, list[str]]:
    """Append `SET <key> "<value>"` for each default whose key isn't set yet (case-insensitive)."""
    present = {m.group(1).decode("latin-1").lower()
               for m in re.finditer(rb"^\s*SET\s+(\S+)", data, re.IGNORECASE | re.MULTILINE)}
    eol = _eol_of(data)
    if data and not data.endswith((b"\n", b"\r")):
        data += eol
    added = []
    for k, v in defaults.items():
        if k.lower() not in present:
            data += f'SET {k} "{v}"'.encode() + eol
            added.append(k)
    return data, added


def configure(client_dir: Path, defaults: dict[str, str] | None = None) -> dict:
    client_dir = Path(client_dir).expanduser()
    if not client_dir.is_dir():
        raise ClientConfigError(f"not a directory: {client_dir}")
    wow = _child_ci(client_dir, "Wow.exe")
    if wow is None or not wow.is_file():
        raise ClientConfigError(f"no Wow.exe in {client_dir}; not a 3.3.5a client dir")
    data_dir = _child_ci(client_dir, "Data")
    if data_dir is None or not data_dir.is_dir():
        raise ClientConfigError(f"no Data/ folder in {client_dir}; not a 3.3.5a client dir")
    locales = sorted(c for c in data_dir.iterdir() if c.is_dir() and LOCALE_RE.match(c.name))
    if not locales:
        raise ClientConfigError(f"no Data/<locale>/ folders (e.g. Data/enUS) in {client_dir}")

    sha_before = _sha256(wow)
    changes = []

    for loc in locales:
        rl = _child_ci(loc, "realmlist.wtf") or (loc / "realmlist.wtf")
        existing = rl.read_bytes() if rl.exists() else None
        changes.append({"kind": "realmlist.wtf", "locale": loc.name,
                        **_write_if_changed(rl, _realmlist_bytes(existing))})

    wtf = _child_ci(client_dir, "WTF") or (client_dir / "WTF")
    cfg = (_child_ci(wtf, "Config.wtf") if wtf.is_dir() else None) or (wtf / "Config.wtf")
    old = cfg.read_bytes() if cfg.exists() else b""
    new, detail = _update_config(old, CONFIG_KEYS)
    if defaults:
        new, detail["defaults_added"] = _add_defaults(new, defaults)
    changes.append({"kind": "Config.wtf", **_write_if_changed(cfg, new), **detail})

    sha_after = _sha256(wow)
    if sha_after != sha_before:   # should be impossible; fail loudly if it ever happens
        raise ClientConfigError(f"Wow.exe hash changed during configure: {sha_before} -> {sha_after}")
    return {
        "client_dir": str(client_dir.resolve()),
        "locales": [l.name for l in locales],
        "changes": changes,
        "changed_files": [c["path"] for c in changes if c["changed"]],
        "wow_exe": {"path": str(wow), "sha256_before": sha_before, "sha256_after": sha_after,
                    "unchanged": sha_before == sha_after},
    }
