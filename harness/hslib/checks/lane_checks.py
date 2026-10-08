"""harness-lanes checks (openspec/changes/second-stack, ADR-0011): lane B's paths and ports, its isolation from the
player and lane A, its refusal outside a harness run, independent `down`, and per-lane orphan healing.

Every check here runs in lane A and drives lane B through `hs` subprocesses with HS_LANE=b (lanestack.run_in_lane).
They sit at order 32-33, before the build/setup barrier (order 34) after which a two-lane run's lane-B worker owns
lane B, so they never race it.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .. import lanestack, paths as P, runreg, soap, stack
from ..registry import check, require

CAP = "harness-lanes"
HS = str(P.HARNESS / "hs")
PLAY = str(P.ROOT / "play")
BOOTSTRAP = str(P.ROOT / "bootstrap")
B = P.layout("b")
A = P.layout("a")


def _b(args: list[str], timeout: float = 900, env: dict | None = None) -> dict:
    """`hs <args>` in lane B; {"cmd", "rc", "out", "err"}."""
    t0 = time.time()
    r = lanestack.run_in_lane("b", args, timeout=timeout, env=env)
    return {"cmd": "HS_LANE=b hs " + " ".join(args), "rc": r.returncode, "s": round(time.time() - t0, 1),
            "out": r.stdout[-3000:], "err": r.stderr[-3000:]}


def _a_env() -> dict:
    return {k: v for k, v in os.environ.items() if k != P.LANE_ENV}


def _lane_pids(lane: str) -> dict:
    return {k: v for k, v in stack.stack_processes(lane).items() if v}


def _ports(lane: str) -> dict:
    return {str(p): stack.port_open(p) for p in P.layout(lane)["ports"].values()}


def _b_answers() -> dict:
    """Lane B observed running: its processes, SOAP `server info` and SQL on its own ports (through lane-B `hs`)."""
    gm = _b(["gm", "server info"], timeout=120)
    sql = _b(["sql", "auth", "SELECT @@port, @@datadir"], timeout=60)
    procs = stack.stack_processes("b")
    return {"processes": procs, "all_three": all(procs.values()), "ports": _ports("b"),
            "soap_ok": gm["rc"] == 0, "soap_out": gm["out"][-300:], "sql": sql["out"].strip(), "sql_rc": sql["rc"],
            "ok": all(procs.values()) and gm["rc"] == 0 and sql["rc"] == 0
                  and sql["out"].split("\t")[0].strip() == str(B["ports"]["mysql"])}


def _a_answers() -> dict:
    procs = stack.stack_processes("a")
    try:
        gm = soap.gm("server info", timeout=30)
        gm_ok, gm_out = gm.ok, gm.output[-300:]
    except OSError as e:
        gm_ok, gm_out = False, str(e)
    return {"processes": procs, "all_three": all(procs.values()), "ports": _ports("a"), "soap_ok": gm_ok,
            "soap_out": gm_out, "ok": all(procs.values()) and gm_ok and stack.port_open(A["ports"]["auth"])}


def _b_clear() -> bool:
    return not any(stack.stack_processes("b").values()) and not any(_ports("b").values())


def _prepare_b(ev: dict) -> None:
    """Lane B built (template, prefix clone), stopped, with a fresh datadir clone."""
    ev["lane_b_ready"] = lanestack.ensure_ready()
    r = _b(["lane", "reset"], timeout=900)
    ev["lane_b_reset"] = {k: r[k] for k in ("cmd", "rc", "s")} | {"tail": (r["out"] + r["err"])[-600:]}
    require(r["rc"] == 0, "GIVEN not met: `hs lane reset` failed", **ev)


def _cleanup_b(ev: dict) -> None:
    if not _b_clear():
        ev.setdefault("cleanup", {})["lane_down"] = runreg.lane_down("b")


def _memory() -> dict:
    out = {}
    try:
        r = subprocess.run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level", "vm.swapusage"],
                           capture_output=True, text=True, timeout=10)
        out["sysctl"] = r.stdout.strip()
        r = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=30)
        out["free_pct"] = (r.stdout.strip().splitlines() or [""])[-1]
    except (OSError, subprocess.SubprocessError) as e:
        out["error"] = str(e)
    return out


# ============================================================================ lane.paths

LANE_A_EXPECTED = {
    "runtime": {"run": P.ENV / "run", "logs": P.ENV / "logs", "tmp": P.ENV / "tmp", "etc": P.SERVER_ETC,
                "mysql_data": P.DATA / "mysql", "my_cnf": P.ENV / "my.cnf", "client_cnf": P.ENV / "client.cnf",
                "client_root_cnf": P.ENV / "client-root.cnf", "wineprefix": P.WINE_LAYERS[P.WINE_LAYER]["prefix"],
                "mysql_socket": P.ENV / "run" / "mysql.sock", "play_lock": P.ENV / "run" / "play.lock"},
    "ports": {"mysql": 3307, "auth": 3724, "world": 8085, "soap": 7878},
}


def _describe(lane: str, render: bool) -> dict:
    env = _a_env() if lane == "a" else None
    code = f"import json; from hslib import lanestack; print(json.dumps(lanestack.describe(render={render})))"
    e = {**(env or os.environ), "PYTHONPATH": str(P.HARNESS)}
    if lane != "a":
        e[P.LANE_ENV] = lane
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, env=e, cwd=P.ROOT)
    if r.returncode != 0:
        return {"error": r.stderr[-2000:], "rc": r.returncode}
    return json.loads(r.stdout)


@check("lane.paths", order=33, capability=CAP, stack="any", tier="quick")
def lane_paths(ctx):
    """second-stack "Lane B paths and ports": with HS_LANE=b inside this harness run, every runtime path the modules
    use (run, logs, tmp, server confs, pid/lock/profile files, my.cnf, client*.cnf, socket, datadir, Wine prefix) is
    under env/lanes/b/, the ports are 3317/3734/8095/7888 (also in the rendered confs and the worldserver's AC_* env),
    and the player paths and run registry are the same as lane A's; lane A's layout is the original one."""
    b, a = _describe("b", True), _describe("a", False)
    ev = {"lane_b": b, "lane_a": {k: a.get(k) for k in ("lane", "runtime", "ports", "player", "run_registry")}}
    require("error" not in b and "error" not in a, "describing a lane failed", **ev)
    root = str(B["root"].resolve()) + "/"
    outside = {k: v for k, v in b["runtime"].items() if not str(Path(v).resolve()).startswith(root)}
    ev["lane_b_outside_root"] = outside
    require(b["lane"] == "b" and not outside, "a lane-B runtime path is outside env/lanes/b/", **ev)
    require(b["ports"] == {"mysql": 3317, "auth": 3734, "world": 8095, "soap": 7888},
            "lane B's ports aren't 3317/3734/8095/7888", **ev)
    require(b["player"] == a["player"] and b["player"]["player_file"] == str(P.ENV / "player.json")
            and b["player"]["settings"] == str(P.ENV / "settings.json")
            and b["player"]["backups"] == str(P.ENV / "backups")
            and b["player"]["play_client"] == str(P.ENV / "client-play"),
            "the player paths differ between the lanes or from env/", **ev)
    require(b["run_registry"] == a["run_registry"] == str(P.ENV / "run" / "harness-runs"),
            "the run registry isn't the shared env/run/harness-runs", **ev)
    require("port=3317" in b["client_cnf_body"], "lane B's client.cnf doesn't use port 3317", **ev)
    # lane A: the original layout
    diff = {k: (a["runtime"].get(k), str(v)) for k, v in LANE_A_EXPECTED["runtime"].items()
            if a["runtime"].get(k) != str(v)}
    ev["lane_a_diff"] = diff
    require(a["lane"] == "a" and not diff and a["ports"] == LANE_A_EXPECTED["ports"],
            "lane A's layout isn't the original one", **ev)
    # lane B's rendered confs and the worldserver's AC_* environment
    rd = b.get("rendered") or {}
    au, wo = rd.get("authserver", {}), rd.get("worldserver", {})
    probs = []
    if au.get("RealmServerPort") != "3734":
        probs.append("authserver RealmServerPort != 3734")
    if wo.get("WorldServerPort") != "8095" or wo.get("SOAP.Port") != "7888":
        probs.append("worldserver WorldServerPort/SOAP.Port != 8095/7888")
    for k in ("LoginDatabaseInfo",):
        if ";3317;" not in (au.get(k) or ""):
            probs.append(f"authserver {k} not on 3317")
    for k in ("LoginDatabaseInfo", "WorldDatabaseInfo", "CharacterDatabaseInfo"):
        if ";3317;" not in (wo.get(k) or ""):
            probs.append(f"worldserver {k} not on 3317")
    for side, d in (("authserver", au), ("worldserver", wo)):
        for k in ("PidFile", "LogsDir", "TempDir"):
            if not (d.get(k) or "").strip('"').startswith(root.rstrip("/")):
                probs.append(f"{side} {k} not under env/lanes/b/")
    env = b.get("spawn_ac_env") or {}
    missing = sorted(set(b.get("ac_env_expected_keys") or []) - set(env))
    if missing or not b.get("ac_env_expected_keys"):
        probs.append(f"worldserver AC_* env lacks {missing or 'everything'}")
    if ";3317;" not in env.get("AC_PLAYERBOTS_DATABASE_INFO", ""):
        probs.append("AC_PLAYERBOTS_DATABASE_INFO not on 3317")
    if env.get("AC_AI_PLAYERBOT_COMMAND_SERVER_PORT") != "0":
        probs.append("AC_AI_PLAYERBOT_COMMAND_SERVER_PORT != 0")
    ev["problems"] = probs
    require(not probs, f"{len(probs)} lane-B config problem(s)", **ev)
    return ev


# ============================================================================ lane.refusal

def _tree_mtimes(root: Path) -> dict:
    out = {}
    if root.exists():
        for dp, dns, fns in os.walk(root):
            for n in dns + fns:
                p = Path(dp) / n
                try:
                    out[str(p.relative_to(root))] = p.lstat().st_mtime_ns
                except FileNotFoundError:
                    pass
    return out


@check("lane.refusal", order=33, capability=CAP, stack="any", tier="quick")
def lane_refusal(ctx):
    """second-stack "Lane B refused outside a harness run" (HS_LANE part): with HS_LANE=b and no live harness run
    (no run id, or a spoofed HS_HARNESS_RUN=1 with a dead run id), `hs up`, `./play`, `hs lane init`, `hs lane reset`,
    `./bootstrap --check` and a nested `hs run` each exit non-zero naming HS_LANE, and nothing in lane B started or
    changed; a bad HS_LANE value is refused too. Control: inside this run, HS_LANE=b passes the guard."""
    outside = {k: v for k, v in os.environ.items() if k not in ("HS_HARNESS_RUN", runreg.ENV_ID, P.LANE_ENV)}
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    spoof = {"HS_HARNESS_RUN": "1", runreg.ENV_ID: f"{dead.pid}-1"}
    cases = [
        ("hs up", [HS, "up"], {P.LANE_ENV: "b"}),
        ("./play", [PLAY, "--account", "test"], {P.LANE_ENV: "b"}),
        ("hs lane init", [HS, "lane", "init"], {P.LANE_ENV: "b"}),
        ("hs lane init (HS_LANE unset)", [HS, "lane", "init"], {}),
        ("hs lane reset", [HS, "lane", "reset"], {P.LANE_ENV: "b"}),
        ("./bootstrap --check", [BOOTSTRAP, "--check"], {P.LANE_ENV: "b"}),
        ("hs run", [HS, "run", "--tier", "quick", "--only", "lane.paths"], {P.LANE_ENV: "b"}),
        ("hs up (spoofed run id)", [HS, "up"], {P.LANE_ENV: "b", **spoof}),
        ("./play (spoofed run id)", [PLAY, "--account", "test"], {P.LANE_ENV: "b", **spoof}),
        ("hs up (HS_LANE=c)", [HS, "up"], {P.LANE_ENV: "c"}),
    ]
    before_tree, t0 = _tree_mtimes(B["root"]), time.time()
    rows, probs = [], []
    for name, cmd, extra in cases:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env={**outside, **extra}, cwd=P.ROOT,
                           stdin=subprocess.DEVNULL)
        out = (r.stdout + r.stderr)
        b_now = _lane_pids("b")
        row = {"case": name, "rc": r.returncode, "names_HS_LANE": "HS_LANE" in out, "output": out[-400:],
               "lane_b_processes": b_now}
        rows.append(row)
        if r.returncode == 0 or not row["names_HS_LANE"] or b_now:
            probs.append(name)
    after_tree = _tree_mtimes(B["root"])
    changed = sorted(k for k in set(before_tree) | set(after_tree) if before_tree.get(k) != after_tree.get(k))
    ctl = subprocess.run([sys.executable, "-c", "import hslib.paths as P; print(P.lane_refusal())"],
                         capture_output=True, text=True, timeout=60, cwd=P.ROOT,
                         env={**os.environ, P.LANE_ENV: "b", "PYTHONPATH": str(P.HARNESS)})
    ev = {"cases": rows, "failed_cases": probs, "lane_b_files_changed": changed[:50],
          "lane_b_ports_after": _ports("b"), "control_inside_run": ctl.stdout.strip() or ctl.stderr[-300:],
          "seconds": round(time.time() - t0, 1)}
    require(not probs, f"not refused (or no HS_LANE in the message, or lane B started): {probs}", **ev)
    require(not changed, "a refused command changed files under env/lanes/b/", **ev)
    require(not any(ev["lane_b_ports_after"].values()), "a lane-B port is open after the refusals", **ev)
    require(ev["control_inside_run"] == "None", "control: HS_LANE=b is refused even inside this harness run", **ev)
    return ev


# ============================================================================ lane.isolation

def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_fp(p: Path) -> dict | None:
    if not p.is_file():
        return None
    st = p.stat()
    return {"bytes": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": _sha(p)}


def _tree_fp(root: Path) -> dict | None:
    """Every file under root: size, mtime, sha256 (None when root doesn't exist)."""
    if not root.is_dir():
        return None
    out = {}
    for dp, dns, fns in os.walk(root):
        for n in fns:
            p = Path(dp) / n
            if p.is_file() and not p.is_symlink():
                out[str(p.relative_to(root))] = _file_fp(p)
    return out


def _fingerprints() -> dict:
    from .. import backups, clientbuild, pristine
    return {"player.json": _file_fp(P.PLAYER_FILE), "settings.json": _file_fp(P.SETTINGS_FILE),
            "backups": backups.fingerprint(P.PLAYER_BACKUPS), "client-play": clientbuild.fingerprint(P.PLAYER_PLAY_CLIENT),
            "client": pristine.fingerprint(P.CLIENT), "lane_a_datadir": _tree_fp(A["mysql_data"])}


def _fp_diff(a: dict, b: dict) -> list[str]:
    out = []
    for k in sorted(set(a) | set(b)):
        if a.get(k) != b.get(k):
            if isinstance(a.get(k), dict) and isinstance(b.get(k), dict):
                sub = sorted(x for x in set(a[k]) | set(b[k]) if a[k].get(x) != b[k].get(x))
                out.append(f"{k}: {sub[:10]}")
            else:
                out.append(k)
    return out


def _grep(path: Path, needle: bytes) -> bool | None:
    try:
        return needle in path.read_bytes()
    except OSError:
        return None


@check("lane.isolation", order=32, capability=CAP, stack="down", tier="full")
def lane_isolation(ctx):
    """second-stack "Lane B leaves the player's files and lane A alone": with fingerprints (size, mtime, SHA-256) of
    env/player.json, env/settings.json, env/backups, env/client-play, client/ and lane A's datadir taken first, lane
    B's stack starts (fresh template clone), a fixture write (a new account) lands in lane B's auth DB on 3317, and
    lane B stops; every fingerprint is unchanged, the account's bytes are in lane B's datadir and not lane A's, and
    lane B's mysqld ran on 3317 with its datadir under env/lanes/b/ and every listener on 127.0.0.1."""
    ev: dict = {"memory_before": _memory()}
    require(not any(stack.stack_processes("a").values()), "GIVEN not met: lane A's stack is running", **ev)
    try:
        _prepare_b(ev)
        marker = "HSLB" + secrets.token_hex(4).upper()          # account names: <= 16 chars, upper case
        acct_file = B["mysql_data"] / "acore_auth" / "account.ibd"
        ev["marker"] = marker
        ev["marker_in_lane_b_before"] = _grep(acct_file, marker.encode())
        t0 = time.time()
        before = _fingerprints()
        ev["fingerprint_s"] = round(time.time() - t0, 1)
        up = _b(["up"], timeout=1800)
        ev["lane_b_up"] = {k: up[k] for k in ("cmd", "rc", "s")} | {"tail": (up["out"] + up["err"])[-800:]}
        require(up["rc"] == 0, "lane B's `hs up` failed", **ev)
        ans = _b_answers()
        ev["lane_b_running"] = ans
        require(ans["ok"], "lane B's stack isn't answering on its own ports", **ev)
        require(str(B["mysql_data"].resolve()) in ans["sql"], "lane B's mysqld datadir isn't env/lanes/b/mysql", **ev)
        pids = [p for v in stack.stack_processes("b").values() for p in v]
        socks = stack.listening_sockets(pids)
        ev["lane_b_listeners"] = socks
        bad = [s for s in socks if not s.split()[-1].startswith("127.0.0.1:")
               or int(s.split(":")[-1]) not in B["ports"].values()]
        require(socks and not bad, "a lane-B listener isn't on 127.0.0.1 and lane B's ports", bad=bad, **ev)
        ev["lane_a_while_b_up"] = _lane_pids("a")
        require(not ev["lane_a_while_b_up"], "lane A's stack runs while only lane B was started", **ev)
        pw = secrets.token_hex(4)
        w = _b(["gm", f"account create {marker} {pw}"], timeout=120)
        ev["fixture_write"] = {"rc": w["rc"], "out": w["out"].replace(pw, "<pw>")[-300:]}
        require(w["rc"] == 0, "the fixture write (account create) failed in lane B", **ev)
        q = _b(["sql", "auth", f"SELECT COUNT(*) FROM account WHERE username='{marker}'"], timeout=60)
        ev["lane_b_has_account"] = q["out"].strip()
        require(q["rc"] == 0 and q["out"].strip() == "1", "lane B's auth DB doesn't have the new account", **ev)
        d = _b(["lane", "down"], timeout=600)
        ev["lane_b_down"] = {"rc": d["rc"], "tail": (d["out"] + d["err"])[-800:]}
        require(d["rc"] == 0 and _b_clear(), "lane B didn't stop cleanly", **ev)
        after = _fingerprints()
        ev["fingerprints_changed"] = _fp_diff(before, after)
        ev["fingerprinted"] = {k: (None if v is None else len(v.get("files", v)) if isinstance(v, dict) else v)
                               for k, v in before.items()}
        require(before["lane_a_datadir"] and before["client"].get("exists"), "GIVEN not met: nothing to fingerprint",
                **ev)
        require(not ev["fingerprints_changed"], "the player's files, client/ or lane A's datadir changed", **ev)
        ev["marker_in_lane_b_after"] = _grep(acct_file, marker.encode())
        ev["marker_in_lane_a"] = _grep(A["mysql_data"] / "acore_auth" / "account.ibd", marker.encode())
        require(ev["marker_in_lane_b_before"] is False and ev["marker_in_lane_b_after"] is True,
                "the write isn't in lane B's datadir (or was there before this run)", **ev)
        require(ev["marker_in_lane_a"] is False, "the lane-B write shows up in lane A's datadir", **ev)
        return ev
    finally:
        _cleanup_b(ev)


# ============================================================================ lane.independent_down

@check("lane.independent_down", order=33, capability=CAP, stack="up", tier="full")
def lane_independent_down(ctx):
    """second-stack "A's down leaves B up, and the reverse": with both lanes' stacks up and answering, lane A's
    `hs down` stops only lane A (lane B's mysqld/auth/world keep their pids and answer SOAP and SQL on 3317/7888);
    then, with lane A restarted, lane B's `hs down` stops only lane B (lane A keeps its pids and answers)."""
    ev: dict = {"memory_before": _memory()}
    try:
        _prepare_b(ev)
        up = _b(["up"], timeout=1800)
        ev["lane_b_up"] = {k: up[k] for k in ("cmd", "rc", "s")} | {"tail": (up["out"] + up["err"])[-800:]}
        require(up["rc"] == 0, "GIVEN not met: lane B's `hs up` failed", **ev)
        a0, b0 = _a_answers(), _b_answers()
        ev["given"] = {"a": a0, "b": b0, "memory": _memory()}
        require(a0["ok"] and b0["ok"], "GIVEN not met: both lanes aren't up and answering", **ev)
        # lane A's down
        r = subprocess.run([HS, "down"], capture_output=True, text=True, timeout=600, env=_a_env())
        a1, b1 = {"processes": _lane_pids("a"), "ports": _ports("a")}, _b_answers()
        ev["after_a_down"] = {"rc": r.returncode, "a": a1, "b": b1, "tail": r.stdout[-600:]}
        require(r.returncode == 0 and not a1["processes"] and not any(a1["ports"].values()),
                "lane A's down didn't stop lane A", **ev)
        require(b1["ok"] and b1["processes"] == b0["processes"], "lane A's down touched lane B", **ev)
        # lane A again, then lane B's down
        r = subprocess.run([HS, "up"], capture_output=True, text=True, timeout=1800, env=_a_env())
        a2 = _a_answers()
        ev["a_restarted"] = {"rc": r.returncode, "a": a2}
        require(r.returncode == 0 and a2["ok"], "lane A didn't come back up", **ev)
        r = _b(["down"], timeout=600)
        a3 = _a_answers()
        b3 = {"processes": _lane_pids("b"), "ports": _ports("b")}
        ev["after_b_down"] = {"rc": r["rc"], "a": a3, "b": b3, "tail": r["out"][-600:]}
        require(r["rc"] == 0 and not b3["processes"] and not any(b3["ports"].values()),
                "lane B's down didn't stop lane B", **ev)
        require(a3["ok"] and a3["processes"] == a2["processes"], "lane B's down touched lane A", **ev)
        return ev
    finally:
        _cleanup_b(ev)


# ============================================================================ lane.orphans

@check("lane.orphans", order=33, capability=CAP, stack="up", tier="full")
def lane_orphans(ctx):
    """second-stack "Orphan healing is per lane": a stand-in lane-B worker (HS_LANE=b, tagged with a stand-in run's
    id) starts lane B's stack; the worker and its run are SIGKILLed, leaving lane B running with a dead run file.
    The next nested `hs run` records lane B's processes as that run's orphans and stops them (`hs lane down`), its
    own check passes, lane B is gone, and lane A (started by this live run) keeps its pids and answers."""
    ev: dict = {"memory_before": _memory()}
    d = Path(tempfile.mkdtemp(prefix="hs-lane-orphans-")).resolve()
    victim = worker = None
    rid = None
    try:
        _prepare_b(ev)
        a0 = _a_answers()
        ev["lane_a_before"] = a0
        require(a0["ok"], "GIVEN not met: lane A isn't up", **ev)
        victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1800)"], start_new_session=True)
        rid = runreg.run_id(victim.pid)
        require(rid, "GIVEN not met: no run id for the stand-in run", **ev)
        (runreg.RUN_DIR / f"{rid}.json").write_text(json.dumps({"id": rid, "pid": victim.pid, "started": "lane.orphans"}))
        env = {**os.environ, P.LANE_ENV: "b", "HS_HARNESS_RUN": "1", runreg.ENV_ID: rid,
               "PYTHONPATH": str(P.HARNESS)}
        worker = subprocess.Popen([sys.executable, "-c", "import time; from hslib import stack; stack.up(); "
                                   "print('up', flush=True); time.sleep(1800)"],
                                  env=env, cwd=P.ROOT, start_new_session=True, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, text=True)
        line = worker.stdout.readline()
        ev["worker"] = {"pid": worker.pid, "said": line.strip(), "run_id": rid}
        b0 = _b_answers()
        tagged = runreg.tagged({rid})
        ev["given"] = {"lane_b": b0, "tagged": tagged, "memory": _memory()}
        bpids = {p for v in b0["processes"].values() for p in v}
        require(line.strip() == "up" and b0["ok"], "GIVEN not met: the stand-in worker didn't bring lane B up", **ev)
        require(bpids and bpids <= {t["pid"] for t in tagged} and all(t["lane"] == "b" for t in tagged
                                                                      if t["pid"] in bpids),
                "GIVEN not met: lane B's processes aren't tagged with the stand-in run", **ev)
        for p in (worker, victim):
            p.kill()
            p.wait()
        ev["killed"] = {"worker_rc": worker.returncode, "victim_rc": victim.returncode,
                        "run_file_left": (runreg.RUN_DIR / f"{rid}.json").exists(), "lane_b_still": _lane_pids("b")}
        require(ev["killed"]["run_file_left"] and ev["killed"]["lane_b_still"],
                "GIVEN not met: the killed worker's lane B isn't left running", **ev)
        t1 = time.time()
        r = subprocess.run([HS, "run", "--tier", "quick", "--only", "lane.paths", "--leave-up", "--results-dir", str(d)],
                           capture_output=True, text=True, timeout=1200, env=_a_env(), cwd=P.ROOT)
        try:
            doc = json.loads((d / "results.json").read_text())
        except (OSError, ValueError):
            doc = {"meta": {}, "checks": []}
        heal = doc["meta"].get("orphans") or {}
        a1 = _a_answers()
        ev["healer"] = {"rc": r.returncode, "s": round(time.time() - t1, 1),
                        "dead_runs": [x.get("id") for x in heal.get("dead_runs", [])],
                        "orphans": heal.get("orphans"), "actions": heal.get("actions"),
                        "remaining": heal.get("remaining"),
                        "checks": {c["id"]: c["status"] for c in doc["checks"]}}
        ev["after"] = {"lane_b": _lane_pids("b"), "lane_b_ports": _ports("b"), "lane_a": a1,
                       "tagged": runreg.tagged({rid}), "run_file_left": (runreg.RUN_DIR / f"{rid}.json").exists()}
        lane_downs = [x["lane_down"] for x in heal.get("actions") or [] if "lane_down" in x]
        require(rid in ev["healer"]["dead_runs"] and {o["pid"] for o in heal.get("orphans") or []} >= bpids,
                "the next run didn't record lane B's processes as the killed run's orphans", **ev)
        require(lane_downs and all(x.get("lane") == "b" and x.get("rc") == 0 for x in lane_downs),
                "the next run didn't stop lane B with `hs lane down`", **ev)
        require(ev["healer"]["checks"].get("lane.paths") == "pass", "the next run's check didn't pass", **ev)
        require(not ev["after"]["lane_b"] and not any(ev["after"]["lane_b_ports"].values())
                and not ev["after"]["tagged"] and not heal.get("remaining"), "lane B's orphans weren't all stopped", **ev)
        require(not ev["after"]["run_file_left"], "the killed run's run file wasn't removed", **ev)
        require(a1["ok"] and a1["processes"] == a0["processes"], "healing lane B touched lane A", **ev)
        return ev
    finally:
        for p in (worker, victim):
            if p is not None and p.poll() is None:
                p.kill()
                p.wait()
        if rid:
            try:
                (runreg.RUN_DIR / f"{rid}.json").unlink()
            except FileNotFoundError:
                pass
        _cleanup_b(ev)
        import shutil
        shutil.rmtree(d, ignore_errors=True)
