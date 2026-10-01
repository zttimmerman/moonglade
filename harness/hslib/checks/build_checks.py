"""server-build checks."""
import os
import subprocess

from .. import build, db, paths as P, soap, stack
from ..registry import check, require


def _git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)


@check("build.pin_enforced", order=10, capability="server-build", stack="down")
def pin_enforced(ctx):
    """`hs build` on dirty + unpinned vendor trees forces them back to fork.lock before building."""
    lock = P.fork_lock()
    # sabotage: move core HEAD off the pin, modify a tracked file, add an untracked file; dirty playerbots too
    r = _git(P.CORE_SRC, "checkout", "-q", "--detach", "HEAD~1")
    require(r.returncode == 0, "could not move core HEAD for the test", stderr=r.stderr)
    (P.CORE_SRC / "AUTHORS").open("a").write("\nharness-sabotage\n")
    (P.CORE_SRC / "hs_sabotage_untracked.txt").write_text("x")
    (P.BOTS_SRC / "hs_sabotage_untracked.txt").write_text("x")
    before = {"core": build.pin_state(P.CORE_SRC, lock["core_sha"], "modules/"),
              "playerbots": build.pin_state(P.BOTS_SRC, lock["playerbots_sha"])}
    require(before["core"] and before["playerbots"], "sabotage did not register as unpinned/dirty", before=before)
    r = subprocess.run([str(P.HARNESS / "hs"), "build"], capture_output=True, text=True)
    info = {"after": {"exit": r.returncode, "stderr_tail": r.stderr[-1000:]},
            "modules": sorted(e.name for e in (P.CORE_SRC / "modules").iterdir() if e.is_symlink())}
    require(r.returncode == 0, "`hs build` failed on a sabotaged vendor tree", **info)
    after = {"core": build.pin_state(P.CORE_SRC, lock["core_sha"], "modules/"),
             "playerbots": build.pin_state(P.BOTS_SRC, lock["playerbots_sha"])}
    ev = {"problems_before": before, "problems_after": after, "sync": info["after"], "modules": info["modules"]}
    require(not after["core"] and not after["playerbots"], "vendor not clean+pinned after sync", **ev)
    require(not (P.CORE_SRC / "hs_sabotage_untracked.txt").exists(), "untracked sabotage file survived", **ev)
    link = P.CORE_SRC / "modules" / "mod-playerbots"
    require(link.is_symlink() and os.readlink(link) == str(P.BOTS_SRC), "mod-playerbots not linked into core", **ev)
    return ev


@check("build.clean", order=20, capability="server-build", stack="down")
def clean_build(ctx):
    """`hs build --clean` from an empty build dir yields executable authserver + worldserver at pinned SHAs."""
    r = subprocess.run([str(P.HARNESS / "hs"), "build", "--clean"], capture_output=True, text=True)
    lock = P.fork_lock()
    heads = {"core": _git(P.CORE_SRC, "rev-parse", "HEAD").stdout.strip(),
             "playerbots": _git(P.BOTS_SRC, "rev-parse", "HEAD").stdout.strip()}
    bins = {n: os.access(P.SERVER_BIN / n, os.X_OK) for n in ("authserver", "worldserver")}
    ev = {"cmd": "harness/hs build --clean", "exit": r.returncode, "stderr_tail": r.stderr[-1500:],
          "binaries_executable": bins, "heads": heads, "pinned": {"core": lock["core_sha"], "playerbots": lock["playerbots_sha"]},
          "build_log": str(P.OUT / "build.log")}
    require(r.returncode == 0, "clean build failed", **ev)
    require(all(bins.values()), "binaries missing or not executable", **ev)
    require(heads["core"] == lock["core_sha"] and heads["playerbots"] == lock["playerbots_sha"], "vendor heads != fork.lock", **ev)
    return ev


@check("build.playerbots_active", order=75, capability="server-build", stack="up")
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
