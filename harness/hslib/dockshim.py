"""The Wine host shim (ADR-0010; openspec/changes/launcher-polish task 2.1): build and locate it.

Source: harness/app/dockshim.m. Output: env/dockshim/hsdock.dylib (universal x86_64 + arm64; the Wine host runs
x86_64 under Rosetta), plus env/dockshim/stamp.json with the source's SHA-256. It lives outside client/ and outside
the pinned runtime env/wowsilicon-r17/ (app.shim_scope). `harness/hs app` builds it; launch.dock_env() rebuilds it
when the source changed (a no-op otherwise). Compiler temp files go to env/dockshim/tmp, removed afterwards.

How it's used: launch.dock_env() puts DYLD_INSERT_LIBRARIES=<this dylib> plus the HSDOCK_* settings into the
environment of the game client's Wine process only (launch.start_client), never globally.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess

from . import paths as P

SOURCE = P.HARNESS / "app" / "dockshim.m"
DIR = P.ENV / "dockshim"
DYLIB = DIR / "hsdock.dylib"
STAMP = DIR / "stamp.json"
LOG = P.LOGS / "dockshim.log"
ARCHS = ("x86_64", "arm64")


class ShimBuildError(RuntimeError):
    pass


def source_sha256() -> str:
    return hashlib.sha256(SOURCE.read_bytes()).hexdigest()


def up_to_date() -> bool:
    try:
        st = json.loads(STAMP.read_text())
    except (OSError, ValueError):
        return False
    return DYLIB.is_file() and st.get("source_sha256") == source_sha256()


def build() -> dict:
    """Compile the shim. Returns evidence; raises ShimBuildError."""
    tmp = DIR / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "TMPDIR": str(tmp) + "/"}
    try:
        sdk = subprocess.run(["/usr/bin/xcrun", "--show-sdk-path"], capture_output=True, text=True, env=env).stdout.strip()
        out = DIR / "hsdock.dylib.new"
        cmd = ["/usr/bin/clang", "-isysroot", sdk, *[x for a in ARCHS for x in ("-arch", a)], "-fobjc-arc", "-O2",
               "-Wall", "-Werror", "-dynamiclib", "-mmacosx-version-min=13.0", "-framework", "Foundation",
               str(SOURCE), "-o", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if r.returncode != 0:
            raise ShimBuildError(f"compiling the Wine host shim failed: {(r.stderr or r.stdout).strip()[-800:]}")
        out.rename(DYLIB)
        st = {"source": str(SOURCE.relative_to(P.ROOT)), "source_sha256": source_sha256(), "archs": list(ARCHS),
              "dylib_sha256": hashlib.sha256(DYLIB.read_bytes()).hexdigest()}
        STAMP.write_text(json.dumps(st, indent=2))
        return {"dylib": str(DYLIB), "cmd": " ".join(cmd), **st}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def ensure_built() -> str:
    """Path of an up-to-date shim, building it if needed."""
    if not up_to_date():
        build()
    return str(DYLIB)
