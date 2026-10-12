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


# Declared ownership (harness-reporting "Impact selection is fail-safe"): what the checks of this file cover.
# First cut generated from the product paths they assert on and the harness modules this file imports directly
# (test-pyramid design 13.1); maintained by hand from here on. Shared infrastructure falls to the smoke set.
COVERS = (
    "harness/hslib/lanes.py",
    "harness/hslib/lanestack.py",
    "harness/hslib/lane.py",
    "harness/hslib/backups.py",
    "harness/hslib/checks/lane_checks.py",
    "harness/hslib/pristine.py",
    "harness/hslib/runreg.py",
)


@check("lane.paths", order=33, capability=CAP, stack="any", layer="unit", parallel_safe=True)
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


@check("lane.refusal", order=33, capability=CAP, stack="any", layer="unit", parallel_safe=False)
def lane_refusal(ctx):
    """second-stack "Lane B refused outside a harness run" (HS_LANE part): with HS_LANE=b and no live harness run
    (no run id, or a spoofed HS_HARNESS_RUN=1 with a dead run id), `hs up`, `./play`, `hs lane init`, `hs lane reset`,
    `./bootstrap --check` and a nested `hs run` each exit non-zero naming HS_LANE, and nothing in lane B started or
    changed; a bad HS_LANE value is refused too. Control: inside this run, HS_LANE=b passes the guard.
    "Test switches can't leak" (scheduler part): with each lane test switch (lanes.TEST_ENV: stubbed memory probe,
    forced lane-B set, worker timeout) set and no harness run, `hs run`, `hs up`, `./play` and `./bootstrap --check`
    each exit non-zero naming the switch, and `hs up` refuses it even with HS_HARNESS_RUN=1 (only a nested `hs run`
    accepts it)."""
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
        ("hs run", [HS, "run", "--tier", "unit", "--only", "lane.paths"], {P.LANE_ENV: "b"}),
        ("hs up (spoofed run id)", [HS, "up"], {P.LANE_ENV: "b", **spoof}),
        ("./play (spoofed run id)", [PLAY, "--account", "test"], {P.LANE_ENV: "b", **spoof}),
        ("hs up (HS_LANE=c)", [HS, "up"], {P.LANE_ENV: "c"}),
    ]
    from .. import lanes as _L
    switch_vals = {_L.MEMORY_ENV: json.dumps({"pressure_level": 1, "avail_gb": 99, "swap_used_gb": 0,
                                              "swap_free_gb": 9}),
                   _L.FORCE_ENV: "era.default_on", _L.TIMEOUT_ENV: "5"}
    for sw in _L.TEST_ENV:
        cases += [
            (f"hs run ({sw})", [HS, "run", "--tier", "unit", "--only", "lane.table"], {sw: switch_vals[sw]}),
            (f"hs up ({sw})", [HS, "up"], {sw: switch_vals[sw]}),
            (f"./play ({sw})", [PLAY, "--account", "test"], {sw: switch_vals[sw]}),
            (f"./bootstrap --check ({sw})", [BOOTSTRAP, "--check"], {sw: switch_vals[sw]}),
            (f"hs up ({sw}, HS_HARNESS_RUN=1)", [HS, "up"], {sw: switch_vals[sw], "HS_HARNESS_RUN": "1"}),
        ]
    before_tree, t0 = _tree_mtimes(B["root"]), time.time()
    a_before = _lane_pids("a")          # a refused command starts (or stops) nothing in lane A either
    rows, probs = [], []
    for name, cmd, extra in cases:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env={**outside, **extra}, cwd=P.ROOT,
                           stdin=subprocess.DEVNULL)
        out = (r.stdout + r.stderr)
        b_now = _lane_pids("b")
        token = next((sw for sw in _L.TEST_ENV if sw in extra), P.LANE_ENV)
        row = {"case": name, "rc": r.returncode, "must_name": token, "names_it": token in out, "output": out[-400:],
               "lane_b_processes": b_now, "lane_a_changed": _lane_pids("a") != a_before}
        rows.append(row)
        if r.returncode == 0 or not row["names_it"] or b_now or row["lane_a_changed"]:
            probs.append(name)
    after_tree = _tree_mtimes(B["root"])
    changed = sorted(k for k in set(before_tree) | set(after_tree) if before_tree.get(k) != after_tree.get(k))
    ctl = subprocess.run([sys.executable, "-c", "import hslib.paths as P; print(P.lane_refusal())"],
                         capture_output=True, text=True, timeout=60, cwd=P.ROOT,
                         env={**os.environ, P.LANE_ENV: "b", "PYTHONPATH": str(P.HARNESS)})
    ctl_sw = {"run_inside_this_run": _L.refusal("run", {**os.environ, _L.MEMORY_ENV: "{}"}),
              "up_inside_this_run": _L.refusal("up", {**os.environ, _L.MEMORY_ENV: "{}"})}
    ev = {"cases": rows, "failed_cases": probs, "lane_b_files_changed": changed[:50], "control_switches": ctl_sw,
          "lane_b_ports_after": _ports("b"), "control_inside_run": ctl.stdout.strip() or ctl.stderr[-300:],
          "seconds": round(time.time() - t0, 1)}
    require(not probs, f"not refused (or no HS_LANE in the message, or lane B started): {probs}", **ev)
    require(not changed, "a refused command changed files under env/lanes/b/", **ev)
    require(not any(ev["lane_b_ports_after"].values()), "a lane-B port is open after the refusals", **ev)
    require(ev["control_inside_run"] == "None", "control: HS_LANE=b is refused even inside this harness run", **ev)
    require(ctl_sw["run_inside_this_run"] is None and ctl_sw["up_inside_this_run"],
            "control: a nested `hs run` doesn't accept the lane test switches (or `hs up` does)", **ev)
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


@check("lane.isolation", order=32, capability=CAP, stack="down", layer="gate")
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

@check("lane.independent_down", order=33, capability=CAP, stack="up", layer="gate")
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

@check("lane.orphans", order=33, capability=CAP, stack="up", layer="gate")
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
        r = subprocess.run([HS, "run", "--tier", "unit", "--only", "lane.paths", "--leave-up", "--results-dir", str(d)],
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


# ============================================================================ scheduler (task groups 2-3)

from .. import lanes as L                      # noqa: E402
from ..registry import REGISTRY                # noqa: E402

HEALTHY = {"pressure_level": 1, "avail_gb": 12.0, "swap_used_gb": 1.0, "swap_free_gb": 3.0}
MERGE_B = ("era.default_on", "era.xp_curve")              # real LANE_B checks (lane B's own stack)
MERGE_A = ("gm.server_info", "sql.world_populated")       # lane-A checks inside lane B's window
MERGE_POST = "client.player_config_untouched"             # order 999: after the join


def _nested(only: list[str], lanes: int, d: Path, extra_env: dict | None = None, tier: str = "full",
            timeout: float = 1800) -> dict:
    """A nested `hs run` (lane A) writing to `d`; {"rc", "s", "doc", "tail"}."""
    t0 = time.time()
    cmd = [HS, "run", "--tier", tier, "--lanes", str(lanes), "--results-dir", str(d)]
    for o in only:
        cmd += ["--only", o]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env={**_a_env(), **(extra_env or {})},
                       cwd=P.ROOT, stdin=subprocess.DEVNULL)
    try:
        doc = json.loads((d / "results.json").read_text())
    except (OSError, ValueError):
        doc = {"meta": {}, "checks": []}
    return {"cmd": " ".join(cmd[1:]).replace(str(P.ROOT) + "/", ""), "env": extra_env or {}, "rc": r.returncode,
            "s": round(time.time() - t0, 1), "doc": doc, "tail": r.stderr[-2500:],
            "started_after": t0}


def _statuses(doc: dict, ids) -> dict:
    return {r["id"]: r["status"] for r in doc.get("checks", []) if r["id"] in ids}


def _lane_summary(n: dict) -> dict:
    """The parts of a nested run's results a lane check asserts on (kept as evidence)."""
    doc = n["doc"]
    ml = doc["meta"].get("lanes") or {}
    return {"cmd": n["cmd"], "env": n["env"], "rc": n["rc"], "s": n["s"], "tail": n["tail"][-800:],
            "records": [(r["id"], r["status"], r.get("lane", "a"), r.get("message", "")[:200]) for r in doc["checks"]],
            "lanes": {k: ml.get(k) for k in ("requested", "lanes", "mode", "assignment", "fallback", "barriers")},
            "preflight": (ml.get("preflight") or {}).get("reasons"),
            "b": {k: (ml.get("b") or {}).get(k) for k in ("wall_s", "rc", "timed_out", "stop", "error", "timeline",
                                                         "started", "joined")} if ml.get("b") else None,
            "timeline_a": ml.get("timeline_a")}


def _ordered(doc: dict) -> bool:
    """Records in run order (runner.run_key; test-pyramid), unit-phase records first."""
    from ..runner import run_key
    ids = [r["id"] for r in doc["checks"] if r["id"] in REGISTRY]
    return ids == sorted(ids, key=lambda i: run_key(REGISTRY[i]))


# ============================================================================ lane.table

@check("lane.table", order=33, capability=CAP, stack="any", layer="unit", parallel_safe=True)
def lane_table(ctx):
    """second-stack "LANE_B table invariants": every LANE_B entry is a registered check with a written reason, and none
    is gui, app.*, client.stock_mode, client.launch_smoke, a baseline/build check, a population/perf check, a
    player-protection check or order >= 998, and each lies inside lane B's window (order 34-379). Self-test: the same
    rules flag a synthetic table holding one check of each forbidden kind and an unregistered id."""
    reg = REGISTRY
    probs = L.table_problems(L.LANE_B, reg)
    no_reason = sorted(i for i, why in L.LANE_B.items() if not (isinstance(why, str) and why.strip()))
    rows = {i: {"order": reg[i].order, "stack": reg[i].stack, "layer": reg[i].layer, "why": L.LANE_B[i]}
            for i in sorted(L.LANE_B) if i in reg}

    def first(pred):
        return next((c.id for c in sorted(reg.values(), key=lambda c: (c.order, c.id)) if pred(c)), None)
    bad = {"gui": first(lambda c: c.gui), "app": first(lambda c: c.id.startswith("app.") and not c.gui),
           "stock_mode": "client.stock_mode", "launch_smoke": "client.launch_smoke",
           "build": first(lambda c: c.id.startswith("build.")), "baseline": "core.login_logout_race_baseline",
           "population": first(lambda c: c.id.startswith("pop.")), "perf": first(lambda c: c.id.startswith("perf.")),
           "order_998": first(lambda c: c.order >= 998), "protection": "play.player_account_untouched",
           "lane_check": "lane.paths", "before_window": first(lambda c: c.order < L.START_ORDER and c.layer == "integration"
                                                             and not c.id.startswith(("build.", "lane.", "setup."))),
           "unregistered": "no.such_check"}
    flagged = {k: [p for p in L.table_problems([i], reg)] if i else None for k, i in bad.items()}
    missed = sorted(k for k, v in flagged.items() if not v)
    ev = {"table": rows, "problems": probs, "no_reason": no_reason, "window": [L.START_ORDER, L.JOIN_ORDER],
          "self_test": {"table": bad, "flagged": flagged, "missed": missed}}
    require(L.LANE_B and not probs, f"LANE_B breaks the table rules: {probs}", **ev)
    require(not no_reason, "a LANE_B entry has no reason", **ev)
    require(not missed, f"self-test: the rules didn't flag {missed}", **ev)
    return ev


# ============================================================================ lane.merge

@check("lane.merge", order=33, capability=CAP, stack="any", layer="gate")
def lane_merge(ctx):
    """second-stack "Two-lane run matches serial statuses": a nested run of two LANE_B checks (era.default_on,
    era.xp_curve), two lane-A checks inside lane B's window (gm.server_info, sql.world_populated) and one after the
    join (client.player_config_untouched, order 999) runs with --lanes 2 (memory probe and battery stubbed healthy, so
    the preflight allows lane B) and then --lanes 1: both exit 0 with every check passing and the same status per
    check; the two-lane run's meta.lanes shows mode 2 with the B checks run in lane B (records marked lane b, a lane-B
    timeline), the records are in (order, id) order in both, and every lane-B check ended before the post-join check
    started; the serial run used one lane. Lane B is down afterwards."""
    ids = MERGE_B + MERGE_A + (MERGE_POST,)
    ev: dict = {"selection": {"b": MERGE_B, "a": MERGE_A, "post_join": MERGE_POST}, "memory_before": _memory()}
    require(all(i in L.LANE_B for i in MERGE_B) and not any(i in L.LANE_B for i in MERGE_A + (MERGE_POST,))
            and L.is_join_point(REGISTRY[MERGE_POST]), "GIVEN not met: the selection doesn't match the LANE_B table",
            **ev)
    base = Path(tempfile.mkdtemp(prefix="hs-lane-merge-")).resolve()
    stub = {L.MEMORY_ENV: json.dumps(HEALTHY), "HS_TEST_BATTERY_SEQ": "100p"}
    try:
        two = _nested(list(ids), 2, base / "two", stub)
        ev["two_lanes"] = _lane_summary(two)
        one = _nested(list(ids), 1, base / "one", {"HS_TEST_BATTERY_SEQ": "100p"})
        ev["one_lane"] = _lane_summary(one)
        s2, s1 = _statuses(two["doc"], ids), _statuses(one["doc"], ids)
        ev["statuses"] = {"two": s2, "one": s1}
        require(two["rc"] == 0 and one["rc"] == 0, "a nested run failed", **ev)
        require(set(s2) == set(ids) and s2 == s1 and all(v == "pass" for v in s2.values()),
                "the two runs' statuses differ (or a check didn't pass)", **ev)
        m2, m1 = two["doc"]["meta"]["lanes"], one["doc"]["meta"]["lanes"]
        b_marked = sorted(r["id"] for r in two["doc"]["checks"] if r.get("lane") == "b")
        ev["b_marked"] = b_marked
        require(m2.get("mode") == 2 and m2["assignment"]["b_ran"] == list(MERGE_B) and not m2["assignment"]["b_to_a"]
                and b_marked == sorted(MERGE_B), "the two-lane run didn't run the B checks in lane B", **ev)
        tl_b = (m2.get("b") or {}).get("timeline") or {}
        tl_a = m2.get("timeline_a") or {}
        require(set(tl_b) == set(MERGE_B) and all(tl_b[i].get("lane") == "b" for i in MERGE_B),
                "no lane-B timeline for the B checks", **ev)
        require(m1.get("mode") == 1 and not any(r.get("lane") == "b" for r in one["doc"]["checks"]),
                "the --lanes 1 run used lane B", **ev)
        require(_ordered(two["doc"]) and _ordered(one["doc"]), "records aren't in run order", **ev)
        b_end = max(tl_b[i]["end"] for i in MERGE_B)
        post_start = (tl_a.get(MERGE_POST) or {}).get("start")
        a_win = [tl_a[i] for i in MERGE_A if i in tl_a]
        ev["join"] = {"b_last_end": b_end, "post_join_start": post_start,
                      "post_join_after_b_s": None if post_start is None else round(post_start - b_end, 2),
                      "overlap_s": round(max(0.0, min(b_end, max((x["end"] for x in a_win), default=0))
                                              - max(min(tl_b[i]["start"] for i in MERGE_B),
                                                    min((x["start"] for x in a_win), default=b_end))), 2),
                      "b_wall_s": m2["b"].get("wall_s"), "two_lane_run_s": two["s"], "one_lane_run_s": one["s"]}
        require(post_start is not None and post_start >= b_end, "a lane-B check ended after the post-join check "
                "started", **ev)
        ev["nested_report_lanes"] = {k: _statuses(n["doc"], ["report.lanes"]) for k, n in (("two", two), ("one", one))}
        require(all(v.get("report.lanes") == "pass" for v in ev["nested_report_lanes"].values()),
                "a nested run's report.lanes didn't pass", **ev)
        ev["lane_b_after"] = {"processes": _lane_pids("b"), "ports": _ports("b")}
        require(_b_clear(), "lane B is still running after the nested runs", **ev)
        return ev
    finally:
        _cleanup_b(ev)
        import shutil
        shutil.rmtree(base, ignore_errors=True)


# ============================================================================ lane.preflight

# forced lane-B set: cheap, stack-free, read-only checks. test-pyramid: acceptance-layer ones (unit-layer checks run in
# the unit layer and never reach the lanes), and outside the release bracket (release.*/firstlaunch.*)
# (test-pyramid part 3: play.startup_failure became a scenario of play.cold_sessions; harness.prefix_scope, a stack-free
# Wine-prefix check, took its place)
FALLBACK_B = ("savesctl.test_actions_refused", "harness.prefix_scope")   # both in lane B's window [34, 380)


def decision_cases() -> dict:
    """lanes.preflight_decision / midrun_stop over readings at and around each ADR-0011 threshold."""
    h = dict(HEALTHY)
    start = {"swap_used_gb": 1.0}
    plug = {"on_battery": False, "percent": 100}
    rows = {
        "healthy_a_up": (h, True, plug, False, True),
        "pressure_2": (h | {"pressure_level": 2}, True, plug, False, False),
        "avail_4.9_a_up": (h | {"avail_gb": 4.9}, True, plug, False, False),
        "avail_5.0_a_up": (h | {"avail_gb": 5.0}, True, plug, False, True),
        "a_stack_down_real_probe": (h | {"avail_gb": 12.0}, False, plug, False, False),
        "swap_free_0.5_grew_1.2": (h | {"swap_free_gb": 0.5, "swap_used_gb": 2.2}, True, plug, False, False),
        "swap_free_0.5_grew_0.5": (h | {"swap_free_gb": 0.5, "swap_used_gb": 1.5}, True, plug, False, True),
        "probe_failed": (h | {"avail_gb": None}, True, plug, False, False),
        "on_battery_90": (h, True, {"on_battery": True, "percent": 90}, False, False),
        "plugged_40": (h, True, {"on_battery": False, "percent": 40}, False, False),
        "on_battery_ignore_power": (h, True, {"on_battery": True, "percent": 90}, True, True),
        "battery_unknown": (h, True, {"on_battery": None, "percent": None}, False, True),
    }
    out = {}
    for k, (now, a_up, batt, ign, want) in rows.items():
        d = L.preflight_decision(now, start, a_up, batt, ign)
        out[k] = {"want_allowed": want, "allowed": d["allowed"], "reasons": d["reasons"], "ok": d["allowed"] == want}
    mid = {"healthy": (h, None), "pressure_2": (h | {"pressure_level": 2}, "pressure"),
           "swap_grew_1.6": (h | {"swap_used_gb": 2.6}, "swap grew"), "swap_grew_1.4": (h | {"swap_used_gb": 2.4}, None),
           "probe_failed": (h | {"pressure_level": None}, "fail closed")}
    for k, (now, want) in mid.items():
        why = L.midrun_stop(now, start)
        out[f"midrun_{k}"] = {"want": want, "stop": why, "ok": (why is None) if want is None else (why is not None and want in why)}
    return out


@check("lane.preflight", order=33, capability=CAP, stack="any", layer="gate")
def lane_preflight(ctx):
    """second-stack "Low memory falls back to one lane" and "Pressure mid-run moves remaining checks to A": the
    preflight/mid-run rules give the ADR-0011 verdict at and around each threshold (pressure, available memory with
    A's stack up, a real reading taken with it down, swap, battery, a failed probe); a nested --lanes 2 run with the memory probe stubbed
    below the threshold runs serially, runs every check, and meta.lanes.fallback names memory; one with the battery
    stubbed at 40 % names power; one whose probe turns to pressure level 2 after lane B's first check lets B run only
    that check, and the rest run in lane A, all passing. (Lane-B set forced to two cheap read-only checks.)"""
    ev: dict = {"decisions": decision_cases()}
    bad = sorted(k for k, v in ev["decisions"].items() if not v["ok"])
    ev["decisions_wrong"] = bad
    base = Path(tempfile.mkdtemp(prefix="hs-lane-preflight-")).resolve()
    force = {L.FORCE_ENV: ",".join(FALLBACK_B)}
    try:
        low = _nested(list(FALLBACK_B), 2, base / "low", force | {
            L.MEMORY_ENV: json.dumps(HEALTHY | {"avail_gb": 2.0}), "HS_TEST_BATTERY_SEQ": "100p"}, timeout=900)
        pwr = _nested(list(FALLBACK_B), 2, base / "power", force | {
            L.MEMORY_ENV: json.dumps(HEALTHY), "HS_TEST_BATTERY_SEQ": "40p"}, timeout=900)
        mid = _nested(list(FALLBACK_B), 2, base / "mid", force | {
            L.MEMORY_ENV: json.dumps({"preflight": HEALTHY, "midrun": [HEALTHY, HEALTHY | {"pressure_level": 2}]}),
            "HS_TEST_BATTERY_SEQ": "100p"}, timeout=900)
        ev["low_memory"], ev["low_power"], ev["midrun"] = _lane_summary(low), _lane_summary(pwr), _lane_summary(mid)
        require(not bad, f"preflight/mid-run rules give the wrong verdict: {bad}", **ev)
        for name, n, word in (("low_memory", low, "memory"), ("low_power", pwr, "power")):
            ml, st = n["doc"]["meta"].get("lanes") or {}, _statuses(n["doc"], FALLBACK_B)
            require(n["rc"] == 0 and st == {i: "pass" for i in FALLBACK_B},
                    f"{name}: not every check ran and passed", **ev)
            require(ml.get("lanes") == 2 and ml.get("mode") == 1 and ml.get("b") is None
                    and not any(r.get("lane") == "b" for r in n["doc"]["checks"]),
                    f"{name}: the run didn't fall back to one lane", **ev)
            require(ml.get("fallback", "").startswith("preflight:") and word in ml["fallback"],
                    f"{name}: meta.lanes.fallback doesn't name the {word} reason", **ev)
        ml = mid["doc"]["meta"].get("lanes") or {}
        st = _statuses(mid["doc"], FALLBACK_B)
        lanes_of = {r["id"]: r.get("lane", "a") for r in mid["doc"]["checks"] if r["id"] in FALLBACK_B}
        stop = (ml.get("b") or {}).get("stop") or {}
        ev["midrun_lanes_of"] = lanes_of
        require(mid["rc"] == 0 and st == {i: "pass" for i in FALLBACK_B}, "mid-run: not every check passed", **ev)
        require(ml.get("mode") == 2 and ml["assignment"]["b_ran"] == [FALLBACK_B[0]]
                and ml["assignment"]["b_to_a"] == list(FALLBACK_B[1:])
                and lanes_of == {FALLBACK_B[0]: "b", **{i: "a" for i in FALLBACK_B[1:]}},
                "mid-run: lane B didn't stop after its first check, or the rest didn't run in lane A", **ev)
        require(stop.get("before_check") == FALLBACK_B[1] and "pressure level 2" in (stop.get("reason") or "")
                and "mid-run" in (ml.get("fallback") or "") and ml.get("fallback_kind") == "midrun",
                "mid-run: the stop isn't recorded with its reason", **ev)
        rl = next((r for r in mid["doc"]["checks"] if r["id"] == "report.lanes"), {})
        ev["midrun_report_lanes"] = {"status": rl.get("status"), "note": (rl.get("evidence") or {}).get("note")}
        require(rl.get("status") == "pass" and "mid-run fallback" in (ev["midrun_report_lanes"]["note"] or ""),
                "mid-run: report.lanes didn't pass with a fallback note", **ev)
        ev["lane_b_after"] = {"processes": _lane_pids("b"), "ports": _ports("b")}
        require(_b_clear(), "lane B is still running after the nested runs", **ev)
        return ev
    finally:
        _cleanup_b(ev)
        import shutil
        shutil.rmtree(base, ignore_errors=True)


# ============================================================================ report.lanes (post-run)

def lanes_problems(doc: dict, wall_s: float, serial_ref: dict | None) -> tuple[list[str], dict]:
    """report.lanes over a results document: meta.lanes agrees with the records (on every run), and an unfiltered
    full/slow run that the preflight let use lane B ran every selected LANE_B check there, with its wall time recorded
    next to the last serial full run's."""
    m = doc["meta"]
    ml = m.get("lanes")
    if not isinstance(ml, dict):
        return ["no meta.lanes in the results"], {}
    recs = [r for r in doc["checks"] if r["id"] in REGISTRY]
    a = ml.get("assignment") or {}
    planned, ran, to_a = a.get("b_planned") or [], a.get("b_ran") or [], a.get("b_to_a") or []
    b_marked = [r["id"] for r in recs if r.get("lane") == "b"]
    ids = [r["id"] for r in recs]
    probs = []
    if len(ids) != len(set(ids)):
        probs.append("a check has more than one record")
    if not _ordered(doc):
        probs.append("records aren't in run order")
    if sorted(b_marked) != sorted(ran):
        probs.append(f"records marked lane b {sorted(b_marked)} != meta.lanes b_ran {sorted(ran)}")
    if set(ran) | set(to_a) != set(planned) or set(ran) & set(to_a):
        probs.append("b_ran + b_to_a isn't exactly b_planned")
    allowed = set(L.LANE_B) if not ml.get("forced") else {x.strip() for x in ml["forced"].split(",")}
    if not set(planned) <= allowed:
        probs.append(f"planned for lane B but not in the table: {sorted(set(planned) - allowed)}")
    tp = L.table_problems(planned, REGISTRY)
    if tp:
        probs.append(f"lane B ran checks the table rules forbid: {tp}")
    power_stop = bool((m.get("power") or {}).get("stop"))
    if not power_stop and [i for i in to_a if i not in ids]:
        probs.append(f"lane-B checks moved to lane A have no record: {[i for i in to_a if i not in ids]}")
    mode = ml.get("mode")
    ev = {"mode": mode, "lanes": ml.get("lanes"), "requested": ml.get("requested"), "fallback": ml.get("fallback"),
          "b_planned": planned, "b_ran": ran, "b_to_a": to_a, "b_marked": b_marked,
          "preflight_reasons": (ml.get("preflight") or {}).get("reasons")}
    if mode == 2:
        b = ml.get("b") or {}
        tl_b, tl_a = b.get("timeline") or {}, ml.get("timeline_a") or {}
        missing_tl = [i for i in ran if i not in tl_b]
        if missing_tl:
            probs.append(f"no lane-B timeline for {missing_tl}")
        b_end = max((tl_b[i]["end"] for i in ran if i in tl_b), default=None)
        join_id = (ml.get("barriers") or {}).get("joined_before")
        post = [i for i, t in tl_a.items() if i in REGISTRY and (L.is_join_point(REGISTRY[i]) or (
            join_id in REGISTRY and (REGISTRY[i].order, i) >= (REGISTRY[join_id].order, join_id)))]
        early = [i for i in post if b_end is not None and tl_a[i]["start"] < b_end]
        if early:
            probs.append(f"checks after the join started before lane B's last check ended: {early}")
        failed = bool(b.get("timed_out") or b.get("error") or b.get("rc") not in (0, None))
        if failed:
            probs.append(f"lane B's worker failed (rc {b.get('rc')}, timed out {b.get('timed_out')}, error "
                         f"{str(b.get('error'))[:200]})")
        # a mid-run memory/power stop is the designed fallback (spec: the remaining B checks run in A): a note, not a
        # failure; checks moved to A without such a recorded reason (and without a worker failure) are unexplained
        stop_reason = (b.get("stop") or {}).get("reason") or ""
        recorded = stop_reason.startswith(("memory:", "power:"))
        if to_a and recorded:
            ev_note = f"mid-run fallback ({stop_reason}): {to_a} ran in lane A"
        else:
            ev_note = None
        if to_a and not recorded and not failed:
            probs.append(f"lane-B checks ran in lane A without a recorded fallback reason: {to_a}")
        ev |= {"b_wall_s": b.get("wall_s"), "b_rc": b.get("rc"), "b_timed_out": b.get("timed_out"),
               "b_stop": b.get("stop"), "b_error": b.get("error"), "post_join_checks": len(post),
               "note": ev_note}
    elif b_marked or ran:
        probs.append("a one-lane run has lane-B records")
    full = m.get("full_run") is True and m.get("tier") == "full" and not m.get("filter")
    ev["full_run"] = full
    if not full:
        ev["full_run_rule"] = "not evaluated: not an unfiltered full run"
        return probs, ev
    ev["wall_s"] = round(wall_s, 1)
    ev["serial_reference"] = serial_ref
    if serial_ref and serial_ref.get("wall_s"):
        ev["wall_vs_serial"] = {"this_run_s": round(wall_s, 1), "last_serial_full_run_s": serial_ref["wall_s"],
                                "saved_s": round(serial_ref["wall_s"] - wall_s, 1)}
    pf = ml.get("preflight") or {}
    if mode == 2:
        missing = sorted(set(L.b_ids([REGISTRY[i] for i in ids], None)) - set(ran) - set(to_a))
        if ml.get("forced"):
            probs.append("a full run used a forced lane-B set")
        if missing:
            probs.append(f"LANE_B checks neither run in lane B nor moved to lane A: {missing}")
        ev["full_run_rule"] = ("evaluated: two-lane full run" + (f"; {ev.get('note')}" if ev.get("note") else
                                                                 "; every LANE_B check ran in lane B"))
    elif ml.get("requested") == 1:
        ev["full_run_rule"] = "not evaluated: --lanes 1 (the serial reference)"
    elif pf and not pf.get("allowed"):
        ev["full_run_rule"] = f"GIVEN not met: the preflight refused lane B ({'; '.join(pf.get('reasons') or [])})"
    else:
        probs.append(f"an unfiltered full run used one lane without a preflight refusal ({ml.get('fallback')})")
    return probs, ev


def synthetic_lanes_cases() -> dict:
    """lanes_problems on two synthetic unfiltered full-run documents (B planned: era.default_on, era.xp_curve):
    (a) a mid-run pressure stop before era.xp_curve, which then ran in A -> no problem, with a note; (b) a killed
    worker (rc -9, no stop reason) whose era.xp_curve ran in A -> a problem."""
    b1, b2 = "era.default_on", "era.xp_curve"

    def doc(b: dict) -> dict:
        recs = [{"id": b1, "status": "pass", "lane": "b"}, {"id": b2, "status": "pass"}]
        ml = {"mode": 2, "lanes": 2, "requested": None, "forced": None, "fallback": "x",
              "assignment": {"b_planned": [b1, b2], "b_ran": [b1], "b_to_a": [b2]}, "preflight": {"allowed": True},
              "barriers": {"joined_before": None}, "timeline_a": {b2: {"start": 20.0, "end": 21.0}},
              "b": {"timeline": {b1: {"start": 1.0, "end": 2.0, "lane": "b"}}, "rc": 0, **b}}
        return {"meta": {"full_run": True, "tier": "full", "filter": [], "lanes": ml}, "checks": recs}
    a_probs, a_ev = lanes_problems(doc({"stop": {"reason": "memory: pressure level 2 (>= 2, warn)",
                                                 "before_check": b2}}), 100.0, None)
    k_probs, _ = lanes_problems(doc({"rc": -9, "stop": None}), 100.0, None)
    out = {"a_midrun_pressure_stop": {"problems": a_probs, "note": a_ev.get("note")},
           "b_killed_worker_no_reason": {"problems": k_probs}}
    out["verdict"] = {"a_passes_with_note": not a_probs and bool(a_ev.get("note")),
                      "b_fails": any("worker failed" in p for p in k_probs)}
    return out


def lanes_record(doc: dict, wall_s: float) -> dict:
    t0 = time.time()
    try:
        ref = L.serial_reference()
    except Exception as e:  # noqa: BLE001
        ref = {"error": f"{type(e).__name__}: {e}"}
    probs, ev = lanes_problems(doc, wall_s, ref)
    syn = synthetic_lanes_cases()
    ev["synthetic"] = syn
    probs += [f"synthetic case {k} has the wrong verdict" for k, ok in syn["verdict"].items() if not ok]
    ok = not probs
    return {"id": "report.lanes", "capability": CAP, "status": "pass" if ok else "fail",
            "evidence": ev | {"problems": probs}, "duration_s": round(time.time() - t0, 2),
            **({} if ok else {"message": "; ".join(probs)[:300]})}
