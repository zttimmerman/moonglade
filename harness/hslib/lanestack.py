"""Lane B, the harness's second isolated test stack (ADR-0011, openspec/changes/second-stack): `hs lane
init|status|down|reset`.

Lane B lives under env/lanes/b/ (paths.layout("b")): its own run/logs/tmp/etc, my.cnf and client*.cnf, ports
3317/3734/8095/7888, a mysqld datadir and a Wine prefix.
- Template datadir env/lanes/b/template: built once by `init` from scratch (`mysqld --initialize-insecure` +
  AzerothCore's AutoSetup, on lane B's ports). The player's datadir (data/mysql) is never read or cloned.
- Datadir env/lanes/b/mysql: an APFS clone (`cp -c`) of the template. `reset` replaces it with a fresh clone, and a
  lane-B `up` with no datadir clones one itself (stack.mysql_up).
- Wine prefix env/lanes/b/<prefix name>: an APFS clone of lane A's, taken only while lane A's Wine is stopped.

`init` and `reset` run only inside a live harness run; `status` and `down` run anywhere (they only look and stop).
Called from lane A, each action re-runs itself as `hs lane <action>` with HS_LANE=b (run_in_lane), so it acts on lane
B's paths and ports.

Hooks for the scheduler (second-stack task group 2): run_in_lane("b", [...]) runs any `hs` command in lane B;
ensure_ready() builds the template once; reset() gives a run a fresh datadir clone; runreg.lane_down("b") stops lane B
from lane A (orphan healing, a worker that timed out).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import paths as P
from .util import log

LANE = "b"
B = P.layout(LANE)
STAMP = ".hs-lane-template.json"


class LaneError(RuntimeError):
    pass


def _under_lane(path: Path) -> Path:
    """`path`, resolved, after asserting it lies inside env/lanes/b/ (every delete here goes through this)."""
    r = Path(path).resolve()
    root = B["root"].resolve()
    if r == root or not r.is_relative_to(root):
        raise LaneError(f"refusing to touch {r}: not inside lane B's root {root}")
    return r


def _rmtree(path: Path) -> None:
    r = _under_lane(path)
    if r.exists() or r.is_symlink():
        shutil.rmtree(r)


def _clone(src: Path, dst: Path) -> dict:
    """APFS clone (cp -c -R) of src to dst (dst must not exist)."""
    _under_lane(dst)
    if dst.exists():
        raise LaneError(f"clone target {dst} already exists")
    dst.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    r = subprocess.run(["/bin/cp", "-c", "-R", str(src), str(dst)], capture_output=True, text=True)
    if r.returncode != 0:
        if dst.exists():
            _rmtree(dst)
        raise LaneError(f"cp -c -R {src} {dst} failed: {r.stderr.strip()[-500:]}")
    return {"from": str(src), "to": str(dst), "s": round(time.time() - t0, 2)}


def template_ready() -> bool:
    t = B["mysql_template"]
    return (t / "mysql").is_dir() and (t / STAMP).is_file()


def _require_lane_b(what: str) -> None:
    if P.LANE != LANE:
        raise LaneError(f"{what} must run with HS_LANE=b (use run_in_lane)")


def _require_run(what: str) -> None:
    if P.live_harness_run() is None:
        raise LaneError(f"{what} refused: lane B (HS_LANE=b) only runs inside a harness run (`harness/hs run`)")


# ---------------------------------------------------------------- lane-B actions (HS_LANE=b)

def clone_datadir() -> dict:
    """Replace lane B's datadir with a fresh APFS clone of the template. Lane B's mysqld must be stopped."""
    from . import stack
    _require_lane_b("clone_datadir")
    if stack.stack_processes()["mysqld"]:
        raise LaneError("lane B's mysqld is running; `hs lane down` first")
    if not template_ready():
        raise LaneError(f"no lane-B template at {B['mysql_template']}; run `harness/hs lane init` in a harness run")
    _rmtree(B["mysql_data"])
    return _clone(B["mysql_template"], B["mysql_data"]) | {"stamp": json.loads((B["mysql_template"] / STAMP).read_text())}


def clone_prefix(force: bool = False) -> dict:
    """Lane B's Wine prefix: an APFS clone of lane A's, taken only while lane A's Wine is stopped."""
    from . import launch
    a_prefix = P.layout("a")["wineprefix"]
    dst = B["wineprefix"]
    if dst.exists() and not force:
        return {"prefix": str(dst), "action": "present"}
    if not (a_prefix / "system.reg").exists():
        return {"prefix": str(dst), "action": "skipped", "why": f"lane A has no Wine prefix at {a_prefix}"}
    running = launch.prefix_processes(prefix=a_prefix)
    if running:
        raise LaneError(f"lane A's Wine is running ({len(running)} process(es)); the prefix is cloned only while "
                        "it is stopped")
    if launch.prefix_processes(prefix=dst):
        raise LaneError("lane B's Wine is running; `hs lane down` first")
    _rmtree(dst)
    return _clone(a_prefix, dst) | {"action": "cloned"}


def down() -> dict:
    """Stop lane B's Wine prefix (if anything runs there) and its stack."""
    from . import launch, stack
    _require_lane_b("lane down")
    out = {"lane": LANE}
    if launch.prefix_processes():
        out["stop_prefix"] = launch.stop_prefix()
    out |= stack.down()
    return out


def status() -> dict:
    from . import launch, stack
    _require_lane_b("lane status")
    st = stack.status()
    stamp = None
    if template_ready():
        try:
            stamp = json.loads((B["mysql_template"] / STAMP).read_text())
        except (OSError, ValueError):
            stamp = "unreadable"
    return {"lane": LANE, "root": str(B["root"]), "ports": B["ports"], **st,
            "template": {"path": str(B["mysql_template"]), "ready": template_ready(), "stamp": stamp},
            "datadir": {"path": str(B["mysql_data"]), "present": (B["mysql_data"] / "mysql").is_dir()},
            "wineprefix": {"path": str(B["wineprefix"]), "present": (B["wineprefix"] / "system.reg").exists(),
                           "processes": launch.prefix_processes()}}


def init(force: bool = False, profile: str = "m0") -> dict:
    """Build lane B: the Wine prefix clone, the template datadir (once; `force` rebuilds it) and a fresh datadir clone.
    The template is built from scratch (mysqld --initialize-insecure, then authserver/worldserver AutoSetup on lane
    B's ports, then a graceful stop), never from the player's data."""
    from . import stack
    _require_lane_b("lane init")
    _require_run("hs lane init")
    out: dict = {"lane": LANE, "down_before": down()}
    out["wineprefix"] = clone_prefix()
    if template_ready() and not force:
        out["template"] = "present"
    else:
        t0 = time.time()
        _rmtree(B["mysql_data"])
        stack.ALLOW_INITIALIZE_LANE_B = True
        try:
            out["build_up"] = stack.up(profile=profile)
        finally:
            stack.ALLOW_INITIALIZE_LANE_B = False
            out["build_down"] = stack.down()
        if out["build_down"].get("remaining"):
            raise LaneError(f"lane B's stack didn't stop after building the template: {out['build_down']}")
        if not (B["mysql_data"] / "acore_world").is_dir():
            raise LaneError("the template build left no acore_world database")
        _rmtree(B["mysql_template"])
        os.replace(B["mysql_data"], _under_lane(B["mysql_template"]))
        stamp = {"built": datetime.now(timezone.utc).isoformat(timespec="seconds"), "profile": profile,
                 "repo_sha": _git_sha(), "s": round(time.time() - t0, 1)}
        (B["mysql_template"] / STAMP).write_text(json.dumps(stamp) + "\n")
        out["template"] = stamp
    out["datadir"] = clone_datadir()
    return out


def reset() -> dict:
    """A fresh lane B: stack down, the datadir replaced by a new template clone."""
    _require_lane_b("lane reset")
    _require_run("hs lane reset")
    return {"lane": LANE, "down": down(), "datadir": clone_datadir()}


def _git_sha() -> str | None:
    r = subprocess.run(["git", "-C", str(P.ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() or None


# ---------------------------------------------------------------- from any lane

def run_in_lane(lane: str, args: list[str], timeout: float | None = None, capture: bool = True,
                env: dict | None = None) -> subprocess.CompletedProcess:
    """`hs <args>` with HS_LANE=<lane> (the caller's environment otherwise, so a harness run's id carries over)."""
    e = {**os.environ, **(env or {}), P.LANE_ENV: lane}
    e["PYTHONPATH"] = str(P.HARNESS) + (":" + e["PYTHONPATH"] if e.get("PYTHONPATH") else "")
    return subprocess.run([sys.executable, "-m", "hslib", *args], cwd=P.ROOT, env=e, timeout=timeout,
                          capture_output=capture, text=True, stdin=subprocess.DEVNULL)


def ensure_ready(timeout: float = 3600) -> dict:
    """From lane A inside a harness run: build lane B's template if there is none (`hs lane init`)."""
    if (B["mysql_template"] / STAMP).is_file() and (B["mysql_template"] / "mysql").is_dir() \
            and (B["wineprefix"] / "system.reg").exists():
        return {"action": "present"}
    r = run_in_lane(LANE, ["lane", "init"], timeout=timeout)
    if r.returncode != 0:
        raise LaneError(f"hs lane init failed (rc {r.returncode}): {(r.stdout + r.stderr)[-2000:]}")
    return {"action": "init", "result": json.loads(r.stdout) if r.stdout.strip().startswith("{") else r.stdout[-2000:]}


def cli(action: str, force: bool = False) -> int:
    if P.LANE == "a":
        if action in ("init", "reset") and P.live_harness_run() is None:
            log(f"`hs lane {action}` refused: lane B (HS_LANE=b) only runs inside a harness run (`harness/hs run`)")
            return 2
        r = run_in_lane(LANE, ["lane", action] + (["--force"] if force else []), capture=False)
        return r.returncode
    if P.LANE != LANE:
        log(f"`hs lane` manages lane B only (HS_LANE={P.LANE})")
        return 2
    from .stack import StackError
    fn = {"init": lambda: init(force=force), "status": status, "down": down, "reset": reset}[action]
    try:
        res = fn()
    except (LaneError, StackError, P.LaneRefused) as e:
        log(f"LANE {action.upper()} FAILED: {e}")
        if getattr(e, "log_excerpt", ""):
            print("----- log excerpt -----\n" + e.log_excerpt, file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2, default=str))
    if action == "down" and res.get("remaining"):
        return 1
    return 0


def describe(render: bool = True) -> dict:
    """This lane's layout as the modules see it (lane.paths runs it with HS_LANE=b): every runtime path, the ports,
    the player paths, the run registry, and (render=True, lane B only) the rendered server confs' lane keys and the
    worldserver's AC_* environment."""
    import re as _re
    from . import backups, clientbuild, config, db, launch, play, runreg, settings, stack
    out = {
        "lane": P.LANE,
        "runtime": {"run": P.RUN, "logs": P.LOGS, "tmp": P.TMP, "etc": P.LANE_ETC, "mysql_data": P.MYSQL_DATA,
                    "my_cnf": stack.MY_CNF, "client_cnf": db.client_cnf(), "client_root_cnf": db.client_cnf(root=True),
                    "wineprefix": P.WINEPREFIX, "wine_env_prefix": launch.wine_env()["WINEPREFIX"],
                    "wine_log": P.WINE_LOG, "profile_file": stack.PROFILE_FILE, "play_lock": play.LOCK,
                    "play_last": play.LAST, "mysql_socket": P.RUN / "mysql.sock",
                    **{f"pidfile_{k}": v for k, v in stack.PIDFILE.items()},
                    **{f"log_{k}": v for k, v in stack.LOG.items()},
                    **{f"errlog_{k}": v for k, v in stack.ERRLOG.items()}},
        "template": P.MYSQL_TEMPLATE,
        "ports": {"mysql": P.MYSQL_PORT, "auth": P.AUTH_PORT, "world": P.WORLD_PORT, "soap": P.SOAP_PORT},
        "player": {"player_file": play.PLAYER_FILE, "settings": settings.FILE, "backups": backups.PLAYER_ROOT,
                   "play_client": clientbuild.PLAY},
        "run_registry": runreg.RUN_DIR,
        "client_cnf_body": "".join(l for l in db.client_cnf().read_text().splitlines(True)
                                   if not l.startswith("password")),
    }
    if render and P.LANE != "a":
        res = config.write_all(profile="m0")

        def opt(f, k):
            m = _re.search(rf"^{_re.escape(k)}\s*=\s*(.*?)\s*$", f.read_text(), _re.M)
            return m.group(1) if m else None
        auth, world = P.LANE_ETC / "authserver.conf", P.LANE_ETC / "worldserver.conf"
        out["rendered"] = {
            "authserver": {k: opt(auth, k) for k in ("RealmServerPort", "LoginDatabaseInfo", "PidFile", "LogsDir",
                                                     "TempDir")},
            "worldserver": {k: opt(world, k) for k in ("WorldServerPort", "SOAP.Port", "LoginDatabaseInfo",
                                                       "WorldDatabaseInfo", "CharacterDatabaseInfo", "PidFile",
                                                       "LogsDir", "TempDir")},
            "modules_mirror": res["modules"], "ac_env_keys": res.get("ac_env"),
        }
        env = stack.spawn_env("worldserver")
        out["spawn_ac_env"] = {k: v for k, v in env.items() if k.startswith("AC_")}
        out["ac_env_expected_keys"] = sorted(config.env_var_name(k) for d in
                                             [config.module_overrides(config.overrides(n)) for n in config.PROFILES]
                                             for v in d.values() for k in v)
    return json.loads(json.dumps(out, default=str))
