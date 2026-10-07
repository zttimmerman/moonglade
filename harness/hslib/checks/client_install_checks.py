"""client-connectivity: the installed client (`hs client-install`) and its launch under Wine (ADR-0004)."""
import fnmatch
import json
import os
import subprocess
from pathlib import Path

from .. import paths as P
from ..registry import check, require

EXPECTED_VERSION = "3.3.5.12340"
# Direct3D/DXGI/DirectInput/sound/proxy wrapper DLLs that must not sit in the client root.
WRAPPER_GLOBS = ["d3d8.dll", "d3d9.dll", "d3d10*.dll", "d3d11.dll", "dxgi.dll", "dinput8.dll",
                 "dsound.dll", "version.dll", "winmm.dll"]


@check("client.inspect", order=205, capability="client-connectivity", stack="any", tier="quick")
def inspect(ctx):
    """Installed client: Wow.exe is 3.3.5.12340, no wrapper DLLs, binaries match the manifest, source untouched."""
    from ..clientinst import load_manifest, sha256_file, source_stat
    from ..pe import PEError, version_info

    client = P.CLIENT
    ev = {"client_dir": str(client), "manifest_path": str(P.CLIENT_MANIFEST)}
    man = load_manifest()
    require(man is not None, "no install manifest; run `harness/hs client-install <zip-or-dir>`", **ev)
    ev["installed_at"] = man.get("installed_at")
    # B32: the manifest names the install folder repo-relative (clientinst.manifest_client_dir), never a stale
    # absolute path from before a repo move/rename
    from ..clientinst import manifest_client_dir
    ev["manifest_client_dir"] = {"recorded": man.get("client_dir"), "expected": manifest_client_dir(client)}
    require(man.get("client_dir") == manifest_client_dir(client),
            f"the install manifest names client_dir {man.get('client_dir')!r}, not {manifest_client_dir(client)!r} "
            "(a stale path; `harness/hs client-install <your client>` rewrites it without changing any client file)", **ev)
    wow = client / "Wow.exe"
    require(wow.is_file(), "client/Wow.exe missing", **ev)

    # 1. PE version resource, read fresh from disk (not from the manifest).
    try:
        vi = version_info(wow)
    except PEError as e:
        require(False, f"cannot parse Wow.exe version resource: {e}", **ev)
    ev["wow_exe_version"] = {"file_version": vi["file_version"], "product_version": vi["product_version"],
                             "FileVersion_string": vi["strings"].get("FileVersion"),
                             "pe_kind": vi["pe_kind"], "machine": vi["machine"]}
    require(vi["file_version"] == EXPECTED_VERSION,
            f"Wow.exe file version is {vi['file_version']}, expected {EXPECTED_VERSION}", **ev)

    # 2. No wrapper DLLs in the client root (case-insensitive).
    root_dlls = sorted(p.name for p in client.iterdir() if p.is_file() and p.suffix.lower() == ".dll")
    wrappers = [n for n in root_dlls if any(fnmatch.fnmatch(n.lower(), g) for g in WRAPPER_GLOBS)]
    ev["root_dlls"], ev["wrapper_globs"], ev["wrapper_dlls_found"] = root_dlls, WRAPPER_GLOBS, wrappers
    require(not wrappers, f"wrapper DLL(s) in client root: {wrappers}", **ev)

    # 3. Every .exe/.dll hash still matches the install manifest.
    recorded = man["binaries_sha256"]
    now, mismatched, missing = {}, [], []
    for rel, h in sorted(recorded.items()):
        p = client / rel
        if not p.is_file():
            missing.append(rel)
            continue
        now[rel] = sha256_file(p)
        if now[rel] != h:
            mismatched.append({"path": rel, "manifest": h, "now": now[rel]})
    on_disk = sorted(p.relative_to(client).as_posix() for p in client.rglob("*")
                     if p.is_file() and p.suffix.lower() in (".exe", ".dll"))
    ev["binaries"] = {"manifest_count": len(recorded), "checked": now,
                      "missing": missing, "mismatched": mismatched,
                      "on_disk_not_in_manifest": [r for r in on_disk if r not in recorded]}
    require(recorded and any(r.lower() == "wow.exe" for r in recorded),
            "manifest has no binary hashes (or no Wow.exe entry)", **ev)
    # An executable or DLL that wasn't there at install time is a client modification (e.g. a patcher's
    # Wow_patched.exe, libDllLdr.dll, mods/*.dll), even though it has no manifest hash to mismatch.
    require(not ev["binaries"]["on_disk_not_in_manifest"],
            "client has .exe/.dll files that weren't present at install time", **ev)
    require(not missing, f"binaries from the manifest are missing: {missing}", **ev)
    require(not mismatched, f"{len(mismatched)} binary hash(es) differ from the manifest", **ev)
    ev["mpq_list"] = {"manifest": [m["path"] for m in man.get("mpqs", [])],
                      "missing_on_disk": [m["path"] for m in man.get("mpqs", []) if not (client / m["path"]).is_file()]}

    # 4. Source byte-identical in size and mtime: install-time before == after, and still so now.
    src = man["source"]
    before, after = src["before"], src["after"]
    ev["source"] = {"path": src["path"], "kind": src["kind"], "before_install": before, "after_install": after}
    require((before["size"], before["mtime_ns"]) == (after["size"], after["mtime_ns"]),
            "source size/mtime changed during the install", **ev)
    # The install-time before/after pair is the proof the install didn't modify the source. Afterwards the source
    # belongs to Zach: if he removed it, record that; if it still exists, it must still match.
    sp = Path(src["path"])
    if not sp.exists():
        ev["source"]["now"] = "removed by its owner after install (not an install effect)"
        return ev
    fresh = source_stat(sp)
    ev["source"]["now"] = fresh
    require((fresh["size"], fresh["mtime_ns"]) == (before["size"], before["mtime_ns"]),
            "source size/mtime differ from before the install", **ev)
    return ev


@check("client.launch_smoke", order=189, capability="client-connectivity", stack="up", tier="full")
def launch_smoke(ctx):
    """`hs launch --smoke`: Wine Wow.exe still running after 30 s, then nothing from this prefix remains."""
    from ..launch import prefix_processes

    cmd = [str(P.HARNESS / "hs"), "launch", "--smoke"]
    # always the real client: a leftover HS_CLIENT_DIR (test-only scratch copy) must not leak into this check
    env = {k: v for k, v in os.environ.items() if k != "HS_CLIENT_DIR"}
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900, env=env)
    try:
        out = json.loads(r.stdout)
    except ValueError:
        out = None
    ev = {"cmd": "harness/hs launch --smoke", "exit": r.returncode,
          "stderr_tail": r.stderr.strip().splitlines()[-15:]}
    if out is None:
        ev["stdout_tail"] = r.stdout[-3000:]
    else:
        ev["result"] = out
    ev["hs_client_dir_in_harness_env"] = os.environ.get("HS_CLIENT_DIR")
    ev["client_dir_used"] = (out or {}).get("client")
    require(r.returncode == 0 and out is not None, "hs launch --smoke failed", **ev)
    require(out.get("client") == str(P.CLIENT) and out.get("client_override") is False,
            "the smoke launch did not run the real client/", **ev)
    sm = out.get("smoke", {})
    require(sm.get("ok") is True, "smoke result not ok", **ev)
    wow_end = sm.get("wow_at_end") or []
    require(sm.get("watched_seconds", 0) >= 30 and wow_end and all(w.get("alive") for w in wow_end),
            "Wow.exe was not alive at the end of a >= 30 s window", **ev)
    # the window is measured from the first sighting of the real Wow.exe process, and it's the same pid at the end
    require(sm.get("wow_pid") and [w.get("pid") for w in wow_end] == [sm["wow_pid"]]
            and out.get("wow_first_seen", {}).get("pid") == sm["wow_pid"],
            "the Wow.exe process alive at the end isn't the one first seen", **ev)
    require(not sm.get("new_error_reports"), "client wrote crash reports", **ev)
    require(out.get("remaining_prefix_processes") == [], "launcher reported leftover prefix processes", **ev)
    after = prefix_processes()          # independent re-check from this process
    ev["prefix_processes_after_check"] = after
    require(after == [], "Wine/Wow.exe processes from env/wineprefix remain", **ev)
    return ev
