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


def ensure_lua() -> dict:
    """The built Lua 5.1.5 interpreter: {"lua": path, "built": bool, "build_s": float}. Builds when the stamp is
    missing or names another archive hash."""
    from . import downloads as D
    want = D.pin("lua51")["sha256"]
    try:
        stamp = json.loads(LUA_STAMP.read_text())
    except (OSError, ValueError):
        stamp = {}
    if stamp.get("sha256") == want and LUA_BIN.is_file():
        return {"lua": str(LUA_BIN), "built": False, "build_s": 0.0}
    t0 = time.time()
    got = D.obtain("lua51")
    if LUA_DIR.exists():
        shutil.rmtree(LUA_DIR)
    LUA_DIR.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(got["path"]) as tf:
        for m in tf.getmembers():
            if m.name != f"lua-{LUA_VERSION}" and not m.name.startswith(f"lua-{LUA_VERSION}/"):
                raise ToolError(f"unexpected member in the Lua archive: {m.name!r}")
            if m.issym() or m.islnk() or ".." in Path(m.name).parts:
                raise ToolError(f"refusing link or parent path in the Lua archive: {m.name!r}")
        tf.extractall(LUA_DIR.parent, filter="data")
    # `posix`, not `macosx`: macosx links readline for the interactive prompt, which the runner never uses.
    r = subprocess.run(["make", "-C", str(LUA_DIR), "posix", f"CC=cc -isysroot {sdk_path()}"],
                       capture_output=True, text=True)
    if r.returncode != 0 or not LUA_BIN.is_file():
        raise ToolError(f"Lua {LUA_VERSION} build failed (rc {r.returncode}): {r.stderr[-2000:]}")
    LUA_STAMP.write_text(json.dumps({"sha256": want, "built_at": time.time()}))
    return {"lua": str(LUA_BIN), "built": True, "build_s": round(time.time() - t0, 2)}


def cpp_inputs() -> list[Path]:
    return sorted([*LOGIC_DIR.glob("*.h"), *LOGIC_DIR.glob("*.cpp"), *CPP_TESTS.glob("*.h"), *CPP_TESTS.glob("*.cpp")])


def cpp_inputs_sha() -> str:
    h = hashlib.sha256()
    h.update(" ".join((CXX, *CXX_FLAGS)).encode())
    for f in cpp_inputs():
        h.update(str(f.relative_to(P.ROOT)).encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def ensure_cpp(force: bool = False) -> dict:
    """The C++ unit test binary: {"bin": path, "built": bool, "build_s": float}."""
    sha = cpp_inputs_sha()
    if not force and CPP_BIN.is_file() and CPP_STAMP.is_file() and CPP_STAMP.read_text().strip() == sha:
        return {"bin": str(CPP_BIN), "built": False, "build_s": 0.0}
    UNIT_OUT.mkdir(parents=True, exist_ok=True)
    srcs = [str(f) for f in cpp_inputs() if f.suffix == ".cpp"]
    t0 = time.time()
    tmp = CPP_BIN.with_name(CPP_BIN.name + ".part")
    r = subprocess.run([CXX, "-isysroot", sdk_path(), *CXX_FLAGS, "-I", str(LOGIC_DIR), "-I", str(CPP_TESTS), *srcs, "-lz", "-o", str(tmp)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise ToolError(f"C++ unit build failed: {r.stderr[-3000:]}")
    tmp.replace(CPP_BIN)
    CPP_STAMP.write_text(sha + "\n")
    return {"bin": str(CPP_BIN), "built": True, "build_s": round(time.time() - t0, 2)}
