"""Unit-layer toolchain (openspec/changes/test-pyramid design 4 and 5).

- Lua 5.1.5 (the client's Lua): the pinned archive (fork.lock lua51_*, downloads.PINS["lua51"]) is verified, unpacked
  into env/tools/lua-5.1.5/ and built with the system `cc`. A stamp beside the build names the archive's SHA-256; a
  missing or mismatched build is rebuilt, never skipped.
- The C++ logic test binary: modules/mod-moonglade/src/logic/*.cpp + harness/tests/unit/cpp/*.cpp, compiled with the
  system clang++ into harness/out/unit/mg_unit, rebuilt only when the SHA-256 of its inputs changes.

Nothing here starts the stack or touches client/, env/player.json or env/settings.json.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

from . import paths as P

LUA_VERSION = "5.1.5"
LUA_DIR = P.ROOT / "env" / "tools" / f"lua-{LUA_VERSION}"
LUA_BIN = LUA_DIR / "src" / "lua"
LUA_STAMP = LUA_DIR / ".hs-lua-stamp.json"

LOGIC_DIR = P.ROOT / "modules" / "mod-moonglade" / "src" / "logic"
CPP_TESTS = P.HARNESS / "tests" / "unit" / "cpp"
UNIT_OUT = P.HARNESS / "out" / "unit"
CPP_BIN = UNIT_OUT / "mg_unit"
CPP_STAMP = UNIT_OUT / "mg_unit.stamp"
CXX = "/usr/bin/clang++"
CXX_FLAGS = ("-std=c++20", "-O0", "-g", "-Wall", "-Wextra", "-Werror")


class ToolError(RuntimeError):
    pass


def sdk_path() -> str:
    """The SDK, passed explicitly: this Mac's stray /usr/local/include shadows the SDK's headers otherwise (as in
    app.py and crashtrace.py)."""
    r = subprocess.run(["/usr/bin/xcrun", "--show-sdk-path"], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        raise ToolError(f"xcrun --show-sdk-path failed: {r.stderr.strip()}")
    return r.stdout.strip()


def ensure_lua(lua_dir: Path | None = None, lock: dict | None = None, cache: Path | None = None) -> dict:
    """The built Lua 5.1.5 interpreter: {"lua": path, "built": bool, "build_s": float}. Builds when the stamp is
    missing or names another archive hash. `lua_dir`, `lock` (a fork.lock mapping) and `cache` (a download cache
    folder) are for report.unit_layer's scratch cases; a pinned archive whose SHA-256 doesn't match raises
    ToolError("pinned download mismatch ...")."""
    from . import downloads as D
    lua_dir = Path(lua_dir or LUA_DIR)
    lua_bin, stamp_f = lua_dir / "src" / "lua", lua_dir / ".hs-lua-stamp.json"
    try:
        want = D.pin("lua51", lock)["sha256"]
    except D.DownloadError as e:
        raise ToolError(f"Lua pin: {e}") from e
    try:
        stamp = json.loads(stamp_f.read_text())
    except (OSError, ValueError):
        stamp = {}
    if stamp.get("sha256") == want and lua_bin.is_file():
        return {"lua": str(lua_bin), "built": False, "build_s": 0.0}
    t0 = time.time()
    try:
        got = D.obtain("lua51", lock, cache)
    except D.DownloadError as e:
        raise ToolError(f"pinned download mismatch: {e}" if e.kind == "damaged" else f"Lua download: {e}") from e
    src_name = f"lua-{LUA_VERSION}"
    work = lua_dir.parent / f".{lua_dir.name}.unpack"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    with tarfile.open(got["path"]) as tf:
        for m in tf.getmembers():
            if m.name != src_name and not m.name.startswith(f"{src_name}/"):
                raise ToolError(f"unexpected member in the Lua archive: {m.name!r}")
            if m.issym() or m.islnk() or ".." in Path(m.name).parts:
                raise ToolError(f"refusing link or parent path in the Lua archive: {m.name!r}")
        tf.extractall(work, filter="data")
    if lua_dir.exists():
        shutil.rmtree(lua_dir)
    (work / src_name).rename(lua_dir)
    shutil.rmtree(work, ignore_errors=True)
    # `posix`, not `macosx`: macosx links readline for the interactive prompt, which the runner never uses.
    r = subprocess.run(["make", "-C", str(lua_dir), "posix", f"CC=cc -isysroot {sdk_path()}"],
                       capture_output=True, text=True)
    if r.returncode != 0 or not lua_bin.is_file():
        raise ToolError(f"Lua {LUA_VERSION} build failed (rc {r.returncode}): {r.stderr[-2000:]}")
    stamp_f.write_text(json.dumps({"sha256": want, "built_at": time.time()}))
    return {"lua": str(lua_bin), "built": True, "build_s": round(time.time() - t0, 2)}


def cpp_inputs(tests: Path | None = None) -> list[Path]:
    t = Path(tests or CPP_TESTS)
    return sorted([*LOGIC_DIR.glob("*.h"), *LOGIC_DIR.glob("*.cpp"), *t.glob("*.h"), *t.glob("*.cpp")])


def cpp_inputs_sha(tests: Path | None = None) -> str:
    h = hashlib.sha256()
    h.update(" ".join((CXX, *CXX_FLAGS)).encode())
    for f in cpp_inputs(tests):
        h.update(str(f.name).encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def ensure_cpp(force: bool = False, tests: Path | None = None, out: Path | None = None) -> dict:
    """The C++ unit test binary: {"bin": path, "built": bool, "build_s": float}. `tests`/`out`: a scratch copy of
    the test folder and where its binary goes (report.unit_layer's scratch cases)."""
    tests = Path(tests or CPP_TESTS)
    out = Path(out or UNIT_OUT)
    binf, stampf = out / "mg_unit", out / "mg_unit.stamp"
    sha = cpp_inputs_sha(tests)
    if not force and binf.is_file() and stampf.is_file() and stampf.read_text().strip() == sha:
        return {"bin": str(binf), "built": False, "build_s": 0.0}
    out.mkdir(parents=True, exist_ok=True)
    srcs = [str(f) for f in cpp_inputs(tests) if f.suffix == ".cpp"]
    t0 = time.time()
    tmp = binf.with_name(binf.name + ".part")
    r = subprocess.run([CXX, "-isysroot", sdk_path(), *CXX_FLAGS, "-I", str(LOGIC_DIR), "-I", str(tests), *srcs,
                        "-lz", "-o", str(tmp)], capture_output=True, text=True)
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise ToolError(f"C++ unit build failed: {r.stderr[-3000:]}")
    tmp.replace(binf)
    stampf.write_text(sha + "\n")
    return {"bin": str(binf), "built": True, "build_s": round(time.time() - t0, 2)}
