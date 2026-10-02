"""server-build checks."""
import json
import os
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import build, db, paths as P, soap, stack
from ..registry import check, require


def _git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)


@check("build.pin_enforced", order=10, capability="server-build", stack="down", tier="full")
def pin_enforced(ctx):
    """`hs build` on dirty + unpinned vendor trees forces them back to fork.lock before building."""
    lock = P.fork_lock()
    # sabotage: move core HEAD off the pin, modify a tracked file, add an untracked file; dirty playerbots too
    # Move HEAD off the pin with an empty commit: a different SHA with no file changes, so restoring the pin
    # doesn't force a rebuild (HEAD~1 here is a 148-file merge; restoring it cost ~5 min of compile).
    r = _git(P.CORE_SRC, "-c", "user.name=hs-sabotage", "-c", "user.email=hs@localhost",
             "commit", "-q", "--allow-empty", "-m", "hs sabotage: unpinned HEAD")
    require(r.returncode == 0, "could not move core HEAD for the test", stderr=r.stderr)
    (P.CORE_SRC / "AUTHORS").open("a").write("\nharness-sabotage\n")
    (P.CORE_SRC / "hs_sabotage_untracked.txt").write_text("x")
    (P.BOTS_SRC / "hs_sabotage_untracked.txt").write_text("x")
    # gitignored sabotage: a real (non-symlink) module dir in core modules/ (would compile in), an ignored file in bots
    rogue = P.CORE_SRC / "modules" / "mod-hs-rogue"
    rogue.mkdir(exist_ok=True)
    (rogue / "CMakeLists.txt").write_text("# rogue module\n")
    (P.BOTS_SRC / "hs_sabotage_ignored.log").write_text("x")
    before = {"core": build.pin_state(P.CORE_SRC, lock["core_sha"], "modules/"),
              "playerbots": build.pin_state(P.BOTS_SRC, lock["playerbots_sha"])}
    before["core_stray_modules"] = build.stray_module_entries()
    require(before["core"] and before["playerbots"] and "mod-hs-rogue" in before["core_stray_modules"],
            "sabotage did not register as unpinned/dirty/stray", before=before)
    r = subprocess.run([str(P.HARNESS / "hs"), "build"], capture_output=True, text=True)
    info = {"after": {"exit": r.returncode, "stderr_tail": r.stderr[-1000:]},
            "modules": sorted(e.name for e in (P.CORE_SRC / "modules").iterdir() if e.is_symlink())}
    require(r.returncode == 0, "`hs build` failed on a sabotaged vendor tree", **info)
    after = {"core": build.pin_state(P.CORE_SRC, lock["core_sha"], "modules/"),
             "playerbots": build.pin_state(P.BOTS_SRC, lock["playerbots_sha"])}
    ev = {"problems_before": before, "problems_after": after, "sync": info["after"], "modules": info["modules"]}
    require(not after["core"] and not after["playerbots"], "vendor not clean+pinned after sync", **ev)
    require(not (P.CORE_SRC / "hs_sabotage_untracked.txt").exists(), "untracked sabotage file survived", **ev)
    require(not rogue.exists(), "rogue (gitignored, non-symlink) module dir survived in core modules/", **ev)
    require(not (P.BOTS_SRC / "hs_sabotage_ignored.log").exists(), "ignored sabotage file survived in playerbots tree", **ev)
    require(not build.stray_module_entries(), "stray entries in core modules/ after build", **ev)
    link = P.CORE_SRC / "modules" / "mod-playerbots"
    require(link.is_symlink() and os.readlink(link) == str(P.BOTS_SRC), "mod-playerbots not linked into core", **ev)
    return ev


def _hs_build(*flags, timeout=5400):
    """Run `harness/hs build <flags>`; return (CompletedProcess, the JSON info it printed or {})."""
    r = subprocess.run([str(P.HARNESS / "hs"), "build", *flags], capture_output=True, text=True, timeout=timeout)
    info = {}
    dec = json.JSONDecoder()
    for i, line in enumerate(r.stdout.splitlines(keepends=True)):
        if line.startswith("{"):
            try:
                info = dec.raw_decode("".join(r.stdout.splitlines(keepends=True)[i:]))[0]
                break
            except ValueError:
                continue
    return r, info


MARKER = ".hs-check-marker"


def _drop_marker() -> str | None:
    """Put a random marker file in build/ before a build: it survives a verified-current (in-place) build and
    disappears in a from-empty one (build/ wiped). None when build/ doesn't exist yet."""
    if not P.BUILD.is_dir():
        return None
    token = secrets.token_hex(8)
    (P.BUILD / MARKER).write_text(token)
    return token


def _marker_state(token: str | None) -> str:
    if token is None:
        return "no build/ before the build"
    m = P.BUILD / MARKER
    if not m.exists():
        return "gone"
    same = m.read_text() == token
    m.unlink()
    return "kept" if same else "changed"


def _summ(info: dict) -> dict:
    """The build's clean-mode record without the bulky per-file digests (those go in counts)."""
    c = dict(info.get("clean", {}))
    for k in ("installed_digest_before", "installed_digest_after"):
        if k in c:
            c[k] = {f: v[:16] for f, v in c[k].items()}
    return c


@check("build.clean", order=20, capability="server-build", stack="down", tier="full")
def clean_build(ctx):
    """`hs build --clean` yields executable authserver + worldserver at pinned SHAs: from empty when the
    build inputs changed (or with --force-clean), else verified-current (stamp == input hash, no-op rebuild)."""
    flags = ["--clean"] + (["--force-clean"] if ctx.get("force_clean") else [])
    token = _drop_marker()
    r, info = _hs_build(*flags)
    marker = _marker_state(token)
    lock = P.fork_lock()
    heads = {"core": _git(P.CORE_SRC, "rev-parse", "HEAD").stdout.strip(),
             "playerbots": _git(P.BOTS_SRC, "rev-parse", "HEAD").stdout.strip()}
    bins = {n: os.access(P.SERVER_BIN / n, os.X_OK) for n in ("authserver", "worldserver")}
    c = info.get("clean", {})
    current = build.input_hash()          # recomputed here, independently of the build process
    ev = {"cmd": "harness/hs build " + " ".join(flags), "exit": r.returncode, "stderr_tail": r.stderr[-1500:],
          "mode": c.get("mode"), "reason": c.get("reason"), "clean": _summ(info),
          "input_hash_recomputed": current, "installed_stamp_now": build.read_stamp(build.install_stamp_path()),
          "build_stamp_now": build.read_stamp(build.build_stamp_path()),
          "build_dir_marker": marker, "binaries_executable": bins, "heads": heads,
          "pinned": {"core": lock["core_sha"], "playerbots": lock["playerbots_sha"]},
          "build_log": str(P.OUT / "build.log")}
    require(r.returncode == 0, "clean build failed", **ev)
    require(all(bins.values()), "binaries missing or not executable", **ev)
    require(heads["core"] == lock["core_sha"] and heads["playerbots"] == lock["playerbots_sha"], "vendor heads != fork.lock", **ev)
    require(c.get("mode") in ("from-empty", "verified-current"), "build reported no clean mode", **ev)
    require(c.get("input_hash") == current, "build's input hash != the hash recomputed by the check", **ev)
    if ctx.get("force_clean"):
        require(c["mode"] == "from-empty", "--force-clean did not do a from-empty build", **ev)
    if c["mode"] == "from-empty":
        require(c.get("build_dir_entries_before_configure") == [], "build dir was not empty before configure", **ev)
        require(marker in ("gone", "no build/ before the build"),
                "from-empty mode but a marker placed in build/ survived (build/ not wiped)", **ev)
        require(ev["installed_stamp_now"] == ev["build_stamp_now"] == current,
                "passing from-empty build did not stamp build/ and the install dir with the input hash", **ev)
    else:
        require(not c.get("from_empty_build_done") and c.get("changed_files") == [],
                "verified-current: the incremental build changed installed files", **ev)
        require(ev["installed_stamp_now"] == current, "verified-current: installed stamp != input hash", **ev)
        require(marker == "kept", "verified-current but build/ was recreated (marker gone)", **ev)
    ctx["build_clean"] = {"mode": c["mode"], "info": info, "ev": ev}
    return ev


def _verified_current_run(ctx) -> tuple[dict, list]:
    """This run's verified-current build: reuse build.clean's when it was one, else run `hs build --clean`
    (a second time if the first had to build from empty, e.g. no stamp yet)."""
    runs = []
    prev = ctx.get("build_clean")
    if prev and prev["mode"] == "verified-current":
        prev["info"]["_check"] = {"build_dir_marker": prev["ev"]["build_dir_marker"]}
        return prev["info"], [{"source": "build.clean in this run", "mode": prev["mode"]}]
    for _ in range(2):
        token = _drop_marker()
        r, info = _hs_build("--clean")
        info["_check"] = {"exit": r.returncode, "stderr_tail": r.stderr[-800:], "build_dir_marker": _marker_state(token)}
        runs.append({"cmd": "harness/hs build --clean", "exit": r.returncode, "mode": info.get("clean", {}).get("mode"),
                     "reason": info.get("clean", {}).get("reason")})
        if r.returncode != 0 or info.get("clean", {}).get("mode") != "from-empty":
            return info, runs
    return info, runs


@check("build.clean_smart", order=21, capability="server-build", stack="down", tier="full")
def clean_smart(ctx):
    """With unchanged inputs, `build.clean` is verified-current (stamp == H, no-op incremental, no from-empty
    build); any changed input (sandbox copies / overrides, never the real modules/) flips the mode to from-empty."""
    info, runs = _verified_current_run(ctx)
    c = info.get("clean", {})
    H = build.input_hash()
    ev = {"builds": runs, "mode": c.get("mode"), "clean": _summ(info), "current_input_hash": H,
          "check_extra": info.get("_check")}
    require(c.get("mode") == "verified-current", "unchanged inputs did not give mode verified-current", **ev)
    require(c["stamps_before"]["build"] == c["stamps_before"]["install"] == H == c.get("input_hash"),
            "GIVEN not met: last from-empty stamp != current input hash", **ev)
    require(c.get("installed_stamp") == H, "installed binaries' stamp != H", **ev)
    digest_files = set(c.get("installed_digest_before", {}))
    require({"bin/authserver", "bin/worldserver"} <= digest_files, "installed digest does not cover the binaries", **ev)
    require(c.get("changed_files") == [] and c["installed_digest_before"] == c["installed_digest_after"],
            "incremental build changed installed binaries", **ev)
    require(c.get("from_empty_build_done") is False and "no from-empty build" in c.get("note", ""),
            "evidence does not state that no from-empty build was done", **ev)
    require((info.get("_check") or {}).get("build_dir_marker") == "kept",
            "build/ was recreated (a marker placed in build/ before the build is gone)", **ev)

    # Part 2: any input change flips the mode. Sandbox copies of the file inputs + in-process overrides for the
    # cmake args and compiler identity; the real modules/, patches/core and fork.lock are never edited.
    real_before = {"modules": build.tree_digest(P.MODULES), "patches": build.tree_digest(P.PATCHES),
                   "fork_lock": build._sha256_file(P.ROOT / "fork.lock")}
    base = build.input_manifest()
    flips = {}
    with tempfile.TemporaryDirectory(prefix="hs-clean-smart-") as td:
        td = Path(td)

        def sandbox():
            for d in ("modules", "patches"):
                shutil.rmtree(td / d, ignore_errors=True)
            shutil.copytree(P.MODULES, td / "modules", symlinks=True)
            shutil.copytree(P.PATCHES, td / "patches", symlinks=True)
            shutil.copy2(P.ROOT / "fork.lock", td / "fork.lock")
            return {"fork_lock_path": td / "fork.lock", "patches_dir": td / "patches", "modules_dir": td / "modules",
                    "cmake": base["cmake_args"], "compiler": base["compiler"]}

        def first_file(d: Path) -> Path:
            files = sorted(p for p in d.rglob("*") if p.is_file() and not p.is_symlink())
            if files:
                return files[0]
            (d / "hs-sandbox-file").write_text("")
            return d / "hs-sandbox-file"

        ov = sandbox()
        copy_hash = build.input_hash(build.input_manifest(**ov))
        ev["sandbox_copy_hash"] = copy_hash
        require(copy_hash == H, "sandbox copy of the inputs does not hash to H (copy not faithful)", **ev)

        def flip(name, ov, what):
            """Hash the (mutated) sandbox inputs right away: the next case re-copies the sandbox."""
            h2 = build.input_hash(build.input_manifest(**ov))
            mode, reason = build.decide_mode(h2, H, H, force=False)
            flips[name] = {"changed": what, "hash": h2, "differs_from_H": h2 != H, "mode": mode, "reason": reason}

        for name, key in (("modules/ (byte appended to a file)", "modules_dir"),
                          ("patches/core/ (byte appended to a file)", "patches_dir"),
                          ("fork.lock (byte appended)", "fork_lock_path")):
            ov = sandbox()
            target = ov[key] if key == "fork_lock_path" else first_file(ov[key])
            with open(target, "ab") as fh:
                fh.write(b"\0")
            flip(name, ov, str(target.relative_to(td)))
        ov = sandbox()
        ov["cmake"] = base["cmake_args"] + ["-DHS_SANDBOX_EXTRA=1"]
        flip("cmake arguments (one appended)", ov, "-DHS_SANDBOX_EXTRA=1")
        ov = sandbox()
        ov["compiler"] = dict(base["compiler"], clang=base["compiler"]["clang"] + " (sandbox)")
        flip("compiler identity (changed)", ov, "clang --version + ' (sandbox)'")
    real_after = {"modules": build.tree_digest(P.MODULES), "patches": build.tree_digest(P.PATCHES),
                  "fork_lock": build._sha256_file(P.ROOT / "fork.lock")}
    ev |= {"input_change_cases": flips, "real_inputs_unchanged": real_before == real_after}
    require(real_before == real_after, "the real build inputs changed during the check", **ev)
    bad = [n for n, f in flips.items() if not f["differs_from_H"] or f["mode"] != "from-empty"]
    require(not bad, f"input change did not flip to from-empty: {bad}", **ev)
    require(build.decide_mode(H, H, H, force=True)[0] == "from-empty", "--force-clean does not force from-empty", **ev)
    return ev


@check("build.playerbots_active", order=75, capability="server-build", stack="up", tier="quick")
def playerbots_active(ctx):
    """mod-playerbots initialized at startup, its DB tables exist, and its GM command is recognized."""
    logtxt = ""
    for f in (P.LOGS / "Server.log", stack.LOG["worldserver"]):
        if f.exists():
            logtxt += f.read_text(errors="replace")
    lines = logtxt.splitlines()
    init_lines = [l for l in lines if "mod-playerbots initialized" in l]
    fatal = [l for l in lines if "playerbot" in l.lower() and any(w in l.lower() for w in ("fatal", "abort", "assert"))]
    tables = int(db.scalar(f"SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='{P.DB_NAMES['playerbots']}'") or 0)
    # `.playerbots pmon` is console-usable and succeeds; an unknown command yields LANG_CMD_INVALID
    # "Command '%s' does not exist" (ChatCommand.cpp) with a SOAP fault.
    res = soap.gm("playerbots pmon")
    ev = {"init_marker_lines": init_lines[:5], "fatal_lines": fatal, "playerbots_db_tables": tables, "gm": res.evidence()}
    require(init_lines, "no 'mod-playerbots initialized' line in worldserver logs", **ev)
    require(not fatal, "fatal playerbots log lines", **ev)
    require(tables > 0, "playerbots DB has no tables", **ev)
    require("does not exist" not in res.output, "playerbots GM command not recognized", **ev)
    require(res.ok, "playerbots GM command did not succeed", **ev)
    return ev
