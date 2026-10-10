"""Unpatched baseline worldserver for `core.login_logout_race_baseline` (openspec/changes/core-server-mail-guard).

The pinned core WITHOUT patches/core/*, built from its own git worktree into its own build dir and install prefix,
all under baseline/ (gitignored). The real vendor tree, build/ and server/ are never written by this module.

  baseline/src      git worktree of vendor/azerothcore-wotlk, detached at fork.lock core_sha, no patches applied;
                    modules/ holds the same symlinks as the real build (mod-playerbots, modules/*), except the
                    modules that need a core patch to compile (NEEDS_CORE_PATCH: they can't build against the
                    unpatched core; the real build keeps them, and fails loudly if the patch is missing)
  baseline/build    cmake build dir (persistent: after the first build, reruns are incremental no-ops)
  baseline/server   cmake install prefix (bin/worldserver, etc/*.dist + confs rendered by config.write_all)
  baseline/logs, baseline/run, baseline/tmp   runtime files of the baseline worldserver

ccache: the source and build paths differ from the real build's, so the first baseline build misses ccache for
every object (absolute paths are part of the hash; base_dir is unset) and compiles from scratch. Later builds are
incremental in baseline/build."""
import os
import re
import shutil
import subprocess
import time

from . import build, config, paths as P, procs, stack
from .util import log

BASE = P.ROOT / "baseline"
SRC = BASE / "src"
BUILD = BASE / "build"
SERVER = BASE / "server"
SERVER_BIN = SERVER / "bin"
SERVER_ETC = SERVER / "etc"
LOGS = BASE / "logs"
RUN = BASE / "run"
TMP = BASE / "tmp"
BUILD_LOG = P.OUT / "baseline-build.log"
SERVER_MAIL_CPP = "src/server/scripts/World/server_mail.cpp"


class BaselineError(RuntimeError):
    def __init__(self, msg: str, **evidence):
        super().__init__(msg)
        self.evidence = evidence


def _git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise BaselineError(f"git {' '.join(args)} failed in {repo}", stderr=r.stderr[-2000:])
    return r


# Modules that only compile against the patched core (true-pause: MoongladePause.cpp includes game/Time/WorldPause.h,
# which patches/core/0003-world-pause.patch adds). The baseline is the core without any patch, so they stay out of it;
# nothing in them takes part in the login/logout race the baseline reproduces.
NEEDS_CORE_PATCH = {"mod-moonglade": "0003-world-pause.patch (MoongladePause.cpp includes WorldPause.h)"}


def sync_src() -> dict:
    """Put baseline/src at core_sha with a clean tree (no patches) and the expected module links."""
    sha = P.fork_lock()["core_sha"]
    BASE.mkdir(exist_ok=True)
    ev = {"src": str(SRC), "core_sha": sha}
    if (SRC / ".git").exists() and _git(SRC, "rev-parse", "HEAD", check=False).returncode != 0:
        log(f"baseline worktree {SRC} is broken; recreating")
        shutil.rmtree(SRC)
    if not (SRC / ".git").exists():
        _git(P.CORE_SRC, "worktree", "prune")
        _git(P.CORE_SRC, "worktree", "add", "--detach", str(SRC), sha)
        ev["worktree"] = "created"
    else:
        _git(SRC, "checkout", "-q", "-f", "--detach", sha)
        _git(SRC, "clean", "-fdx", "-e", "/modules/")
        ev["worktree"] = "reset"
    mods = SRC / "modules"
    links = {k: v for k, v in build._expected_links().items() if k not in NEEDS_CORE_PATCH}
    ev["modules_excluded"] = {k: v for k, v in NEEDS_CORE_PATCH.items() if k in build._expected_links()}
    tracked = {l.split("/", 1)[1].split("/")[0] for l in _git(SRC, "ls-files", "modules").stdout.splitlines()}
    for e in sorted(mods.iterdir()):
        if e.name in tracked or (e.is_symlink() and e.name in links and os.readlink(e) == str(links[e.name])):
            continue
        shutil.rmtree(e) if e.is_dir() and not e.is_symlink() else e.unlink()
    for name, target in links.items():
        dst = mods / name
        if not dst.is_symlink():
            dst.symlink_to(target)
    ev["modules"] = sorted(links)
    ev |= pristine_state()
    return ev


def pristine_state() -> dict:
    """Evidence that baseline/src is the pinned core with no patch: HEAD, git status, and server_mail.cpp vs core_sha."""
    sha = P.fork_lock()["core_sha"]
    status = [l for l in _git(SRC, "status", "--porcelain", "--ignored=no").stdout.splitlines()
              if not l[3:].startswith("modules/")]
    pinned = _git(SRC, "show", f"{sha}:{SERVER_MAIL_CPP}").stdout
    current = (SRC / SERVER_MAIL_CPP).read_text()
    return {"head": _git(SRC, "rev-parse", "HEAD").stdout.strip(), "status_porcelain": status,
            "server_mail_cpp_equals_core_sha": current == pinned,
            "server_mail_cpp_has_guid_guard": "player->GetGUID() != guid" in current}


def cmake_args() -> list[str]:
    return [f"-DCMAKE_INSTALL_PREFIX={SERVER}" if str(a).startswith("-DCMAKE_INSTALL_PREFIX=") else str(a)
            for a in build.cmake_args()]


def discard_foreign_build(build_dir=None, src=None) -> dict | None:
    """F30 (easy-setup): a baseline build tree whose CMakeCache was made in another folder (the repo moved or was
    renamed) can't be reused (cmake refuses it, and its paths are baked in): remove it so the build starts fresh.
    Returns what was discarded, or None."""
    build_dir, src = build_dir or BUILD, src or SRC
    foreign = build.foreign_build_tree(build_dir, src)
    if not foreign:
        return None
    log(f"baseline build tree {build_dir} was made in another folder ({foreign.get('cache_dir')}); discarding it")
    shutil.rmtree(build_dir)
    return foreign | {"discarded": str(build_dir), "removed": not build_dir.exists()}


def build_install(jobs=None, build_dir=None, src=None, configure_only=False, log_path=None,
                  ignore_power=False) -> dict:
    """cmake configure + build + install into baseline/ (incremental after the first time). Log: BUILD_LOG.
    A build tree made in another folder is discarded first (discard_foreign_build). `build_dir`/`src`/`configure_only`
    let build.moved_repo exercise that on a scratch build dir without building."""
    from . import power
    jobs, jobs_why = power.resolve_jobs(jobs)        # power-guard: performance cores unless given
    bdir, sdir = build_dir or BUILD, src or SRC
    P.OUT.mkdir(parents=True, exist_ok=True)
    discarded = discard_foreign_build(bdir, sdir)
    bdir.mkdir(parents=True, exist_ok=True)
    first = not (bdir / "CMakeCache.txt").exists()
    t0 = time.time()
    steps = []
    plan = [["cmake", "-S", str(sdir), "-B", str(bdir), *cmake_args()]]
    if not configure_only:
        plan += [["cmake", "--build", str(bdir), "-j", str(jobs)], ["cmake", "--install", str(bdir)]]
    blog = log_path or BUILD_LOG
    names = ["the configure step (cmake)", "the compile step (cmake --build)", "the install step (cmake --install)"]
    with open(blog, "w") as fh:
        def sink(m):
            fh.write(m + "\n")
            fh.flush()
        sink(power.jobs_line(jobs, jobs_why))
        log(power.jobs_line(jobs, jobs_why))
        guard = power.Guard("the baseline server build", "the harness run", ignore_power, say=log, sink=sink)
        for name, step in zip(names, plan):
            try:
                guard.check(name)            # power-guard: pause on low battery; a stop propagates (PowerStop)
            except power.PowerStop as e:
                e.guard = guard.record()
                raise
            s0 = time.time()
            log("$ " + " ".join(step) + f"   (log: {blog})")
            fh.write("\n$ " + " ".join(step) + "\n")
            fh.flush()
            r = subprocess.run(step, stdout=fh, stderr=subprocess.STDOUT)
            steps.append({"cmd": step[:3], "exit": r.returncode, "s": round(time.time() - s0, 1)})
            if r.returncode != 0:
                raise BaselineError(f"baseline build step failed: {' '.join(step[:3])}; see {blog}", steps=steps,
                                    discarded_foreign_build=discarded)
    if configure_only:
        return {"first_build": first, "steps": steps, "wall_s": round(time.time() - t0, 1), "log": str(blog),
                "discarded_foreign_build": discarded, "cache": build.cmake_cache_info(bdir),
                "jobs": {"n": jobs, "why": jobs_why}, "power": guard.record()}
    wb = SERVER_BIN / "worldserver"
    if not os.access(wb, os.X_OK):
        raise BaselineError("baseline worldserver missing after install", steps=steps)
    return {"first_build": first, "steps": steps, "wall_s": round(time.time() - t0, 1), "log": str(blog),
            "worldserver": str(wb), "worldserver_sha256": build._sha256_file(wb), "discarded_foreign_build": discarded,
            "jobs": {"n": jobs, "why": jobs_why}, "power": guard.record()}


def worldserver_pids() -> list[int]:
    return [pid for pid, cmd in procs.table() if cmd.startswith(str(SERVER_BIN / "worldserver"))]


def start_worldserver(timeout=600, extra_env: dict | None = None) -> tuple[subprocess.Popen, dict]:
    """Render baseline confs (same DBs/ports/overrides as the real stack, own logs/pidfile) and start the baseline
    worldserver, with `extra_env` added to its environment (e.g. crashtrace's DYLD_INSERT_LIBRARIES). The real
    worldserver must already be stopped (same world + SOAP ports)."""
    if stack.stack_processes()["worldserver"]:
        raise BaselineError("the real worldserver is still running; stop it before starting the baseline")
    config.write_all(etc=SERVER_ETC, logs=LOGS, run=RUN, tmp=TMP, source=SRC)
    out, err = open(LOGS / "worldserver.out", "w"), open(LOGS / "worldserver.err", "w")
    proc = subprocess.Popen([str(SERVER_BIN / "worldserver"), "-c", str(SERVER_ETC / "worldserver.conf")],
                            cwd=SERVER_BIN, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True,
                            env={**os.environ, **(extra_env or {})})
    t0 = time.time()
    ok = stack._wait(stack.soap_ready, timeout, interval=2.0, watch_pid=proc.pid)
    ev = {"pid": proc.pid, "ready": ok, "ready_s": round(time.time() - t0, 1),
          "cmdline_is_baseline": proc.pid in worldserver_pids()}
    if not ok:
        stop_worldserver(proc)
        raise BaselineError("baseline worldserver did not become ready", **ev,
                            log_tail=_tail(LOGS / "worldserver.out") + _tail(LOGS / "worldserver.err"))
    return proc, ev


def stop_worldserver(proc: subprocess.Popen) -> dict:
    """Stop the baseline worldserver if it is still alive; always reap it."""
    ev = {"pid": proc.pid, "was_alive": stack.alive(proc.pid)}
    if ev["was_alive"]:
        ev |= stack.stop_worldserver(proc.pid)
    try:
        ev["returncode"] = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        ev["returncode"] = proc.wait(timeout=10)
    for pid in worldserver_pids():          # belt and braces: nothing from baseline/ survives
        os.kill(pid, 9)
        ev.setdefault("killed_strays", []).append(pid)
    return ev


def _tail(path, n=30) -> str:
    try:
        return "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", open(path, errors="replace").read()).splitlines()[-n:])
    except OSError:
        return ""
