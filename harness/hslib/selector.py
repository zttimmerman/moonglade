"""Impact selection (openspec/changes/test-pyramid design 8, spec "Impact selection is fail-safe"). PROTOTYPE (task 1.3).

`select(paths)` maps a change's paths to the acceptance tests a `pr` run executes, or FULL. Rules, in spec order:
1. FULL-trigger paths -> FULL.
2. No-impact paths -> nothing beyond the unit layer.
3. Otherwise the tests whose `covers` globs match the path or whose own file's import closure contains it; plus
   journey.first_play whenever a journey's import closure contains it (and whenever a journey is selected at all, as
   the spec's MoongladePause.cpp example requires). A path no test owns -> FULL ("unmapped path").
4. config.py / paths.py: if only top-level assignments differ, the tests whose closure files mention a changed name;
   anything else -> FULL.
A git failure (paths None), an unparseable source or a stale covers glob -> FULL.

Until task 2.1 puts `layer` and `covers` on every check, the prototype takes the target layout from design.md
appendix A ("Goes to") and the covers below (by check file, plus the journeys' covers from design 10). The journey
check files don't exist yet, so a journey's import closure is modelled as the union of the closures of the files of
the checks it replaces. `JOURNEY_RULE = "design"` switches to design 8's narrower variant (a journey whose closure
merely contains the path is replaced by journey.first_play).
"""
from __future__ import annotations

import ast
import fnmatch
import inspect
import re
import subprocess
import time
from pathlib import Path

from . import paths as P

FULL_TRIGGERS = ("patches/core/**", "patches/playerbots/**", "fork.lock", "bootstrap", "harness/hslib/build.py",
                 "harness/hslib/stack.py", "harness/hslib/probe/**")
CONST_FILES = ("harness/hslib/config.py", "harness/hslib/paths.py")
NO_IMPACT = ("openspec/**", "docs/**", "*.md", "harness/tests/unit/**")

JOURNEYS = {"J1": "journey.first_play", "J2": "journey.pause", "J3": "journey.saves", "J4": "journey.login_recovery",
            "J5": "journey.stock_client", "J6": "journey.app_desktop"}
JOURNEY_COVERS = {   # design 10
    "journey.first_play": ("play", "harness/app/*", "client-mods/**", "patches/client/**"),
    "journey.pause": ("client-mods/addons/Moonglade/*", "modules/mod-moonglade/src/MoongladePause.cpp"),
    "journey.saves": ("client-mods/glue/HSCharSaves.lua", "client-mods/glue/HSFrontMenu.*",
                      "client-mods/addons/Moonglade/MoongladeSaves.lua", "modules/mod-moonglade/src/MoongladeSaves.*",
                      "modules/mod-moonglade/src/logic/Saves*"),
    "journey.login_recovery": ("client-mods/glue/HSSeamless.lua", "client-mods/glue/HSFrontMenu.*"),
    "journey.stock_client": ("patches/client/**",),
    "journey.app_desktop": ("harness/app/*", "branding/*"),
}

MOD = "modules/mod-moonglade/"
# (logic/Pause* and logic/Dungeons* join these maps when PauseLogic and DungeonsLogic exist: a glob matching no file
# is a stale map, which selects FULL.)
# Prototype covers (non-Python paths a check asserts on), by check file. Task 2.1 moves these onto each check.
FILE_COVERS = {
    "app_checks.py": ("harness/app/*", "branding/*", "play", "harness/hslib/camask.py"),
    "backup_checks.py": ("play",),
    "build_checks.py": ("modules/**", "patches/**", "Brewfile", "harness/hs"),
    "client_checks.py": ("patches/client/**",),
    "client_install_checks.py": ("patches/client/**",),
    "client_play_checks.py": ("patches/client/**", "client-mods/**"),
    "companion_checks.py": (MOD + "conf/*",),
    "control_checks.py": (MOD + "src/MoongladeAddon.*", MOD + "src/mod_moonglade_loader.cpp", MOD + "conf/*"),
    "core_checks.py": ("patches/core/**", "patches/playerbots/**"),
    "dungeon_checks.py": (MOD + "src/MoongladeDungeons.cpp", MOD + "conf/*",
                          MOD + "src/mod_moonglade_loader.cpp"),
    "era_checks.py": ("modules/mod-era-profile/**", "tools/era-diff/**"),
    "firstlaunch_checks.py": ("harness/app/launcher.m",),
    "fixture_checks.py": (),
    "lane_checks.py": (),
    "lifecycle_checks.py": ("harness/hs", "play", "harness/hslib/cli.py", "harness/hslib/__main__.py"),
    "login_checks.py": ("client-mods/glue/**", "patches/client/**"),
    "loop_checks.py": ("harness/hs", "harness/hslib/cli.py", "harness/hslib/__main__.py"),
    "naming_checks.py": (),
    "pause_checks.py": (MOD + "src/MoongladePause.cpp", MOD + "src/MoongladeAddon.*",
                        MOD + "src/mod_moonglade_loader.cpp", MOD + "conf/*", "client-mods/addons/Moonglade/*"),
    "play_checks.py": ("play",),
    "polish_checks.py": ("harness/app/*", "branding/*", "play", "harness/hslib/checks/lssampler.m"),
    "population_checks.py": (),
    "power_checks.py": (),
    "pristine_checks.py": (".claude/hooks/*", ".claude/settings.json"),
    "public_checks.py": ("LICENSE", "NOTICE", "tools/go-public/**"),
    "re_checks.py": ("harness/ghidra_scripts/**",),
    "release_checks.py": ("tools/release/**", ".github/workflows/*", "harness/hslib/checks/release_paths_golden.json"),
    "release_runtime_checks.py": ("tools/release/**", ".github/workflows/*"),
    "reporting.py": ("harness/active-changes", "harness/results.schema.json", "harness/timing-baseline.json"),
    "resolution_checks.py": ("patches/client/**",),
    "saves_checks.py": (MOD + "src/MoongladeSaves.*", MOD + "src/logic/Saves*", MOD + "src/MoongladeAddon.*",
                        MOD + "src/mod_moonglade_loader.cpp", MOD + "conf/*", "client-mods/addons/Moonglade/*"),
    "saves_gui_checks.py": ("client-mods/glue/**",),
    "savesctl_checks.py": ("client-mods/glue/**", "play"),
    "scope_checks.py": (MOD + "src/MoongladeQuiet.cpp",),
    "seamless_checks.py": ("client-mods/glue/**", MOD + "src/MoongladeQuiet.cpp"),
    "setup_checks.py": ("Brewfile", "bootstrap"),
    "signing_checks.py": ("tools/release/**", ".github/workflows/*", "branding/*"),
}

JOURNEY_RULE = "spec"     # "spec" (rule 3 as written) or "design" (design 8: closure-only journeys -> first_play)


# ---------------------------------------------------------------- the target layout (prototype: design.md appendix A)

def plan(reg: dict | None = None) -> dict:
    """{test id: {"layer", "from": [check ids], "files": {check files}, "covers": (globs)}} for every acceptance-
    layer test after migration (integration, journey, gate), plus "unit_owned": {check file: [unit check ids]}."""
    from .registry import load_all
    reg = reg if reg is not None else load_all()
    text = (P.ROOT / "openspec/changes/test-pyramid/design.md").read_text()
    goes = {i: g.strip() for i, g in re.findall(r"^\| `([a-z_.0-9]+)` \| [\d.]+ \| \w+ \| ([^|]+) \|", text, re.M)}
    tests: dict = {}
    unit_owned: dict = {}
    for cid, c in reg.items():
        f = Path(inspect.getsourcefile(c.fn))
        rel = str(f.relative_to(P.ROOT))
        g = goes.get(cid)
        if g is None:                                   # checks newer than the appendix (m4-dungeon-scaling)
            g = "gate" if c.tier == "slow" else "integration" if c.stack == "up" else "unit"
        m = re.match(r"merged into `([^`]+)`", g)
        if m:
            tid, layer = m.group(1), "integration"
        elif g in JOURNEYS:
            tid, layer = JOURNEYS[g], "journey"
        elif g.startswith("integration"):
            tid, layer = cid, "integration"
        elif g == "gate":
            tid, layer = cid, "gate"
        else:                                           # unit, run guard, dropped: never selected by paths
            unit_owned.setdefault(rel, []).append(cid)
            continue
        t = tests.setdefault(tid, {"layer": layer, "from": [], "files": set(), "covers": ()})
        t["from"].append(cid)
        t["files"].add(rel)
        cov = JOURNEY_COVERS.get(tid) if layer == "journey" else FILE_COVERS.get(f.name, ())
        t["covers"] = tuple(dict.fromkeys(t["covers"] + tuple(cov or ())))
    return {"tests": tests, "unit_owned": unit_owned}


# ---------------------------------------------------------------- selection

def _match(path: str, globs) -> bool:
    return any(fnmatch.fnmatchcase(path, g) or (g.endswith("/**") and path.startswith(g[:-2])) for g in globs)


def _closures(pl: dict, root: Path) -> dict:
    """check file -> set of repo paths in its import closure (cached per run)."""
    from .runner import import_closure
    files = {f for t in pl["tests"].values() for f in t["files"]} | set(pl["unit_owned"])
    return {f: set(import_closure(roots=(f,), root=root)) for f in files}


def const_change(old_src: str | None, new_src: str) -> set[str] | None:
    """Rule 4: the top-level names whose assignments differ when nothing else differs; None when anything else
    changed (or a source doesn't parse)."""
    if old_src is None:
        return None
    try:
        a, b = ast.parse(old_src), ast.parse(new_src)
    except SyntaxError:
        return None

    def split(tree):
        assigns, other = {}, []
        for n in tree.body:
            targets = (n.targets if isinstance(n, ast.Assign) else [n.target] if isinstance(n, (ast.AnnAssign, ast.AugAssign)) else None)
            if targets and all(isinstance(t, ast.Name) for t in targets):
                for t in targets:
                    assigns[t.id] = ast.dump(n)
            else:
                other.append(ast.dump(n))
        return assigns, other
    (aa, ao), (ba, bo) = split(a), split(b)
    if ao != bo:
        return None
    return {k for k in set(aa) | set(ba) if aa.get(k) != ba.get(k)}


def select(paths: list[str] | None, root: Path | None = None, reg: dict | None = None, pl: dict | None = None,
           closures: dict | None = None, old_source=None, journey_rule: str | None = None, new_source=None) -> dict:
    """{"full": bool, "full_reasons": [...], "selected": {test id: [reasons]}, "duration_s"}.
    `old_source(path) -> str | None`: the path's text before the change (rule 4); None means unknown."""
    t0 = time.perf_counter()
    root = Path(root or P.ROOT)
    journey_rule = journey_rule or JOURNEY_RULE
    out = {"full": False, "full_reasons": [], "selected": {}, "unit_only": [], "paths": paths}

    def full(why):
        out["full"] = True
        out["full_reasons"].append(why)

    if paths is None:
        full("git failed: the changed paths are unknown")
        out["duration_s"] = round(time.perf_counter() - t0, 3)
        return out
    try:
        pl = pl or plan(reg)
        closures = closures if closures is not None else _closures(pl, root)
    except SyntaxError as e:
        full(f"unparseable source: {e}")
        out["duration_s"] = round(time.perf_counter() - t0, 3)
        return out
    tests = pl["tests"]
    # stale covers: a glob that matches no file in the checkout
    all_files = None
    for tid, t in tests.items():
        for g in t["covers"]:
            if any(ch in g for ch in "*?["):
                if all_files is None:
                    all_files = _repo_files(root)
                if not any(_match(f, (g,)) for f in all_files):
                    full(f"stale impact map: {tid} covers {g!r}, which matches no file")
            elif not (root / g).exists():
                full(f"stale impact map: {tid} covers {g!r}, which doesn't exist")

    sel = out["selected"]

    def add(tid, why):
        sel.setdefault(tid, []).append(why)

    def owners_of(path: str, names: set[str] | None = None) -> bool:
        hit = False
        for tid, t in tests.items():
            by_cov = _match(path, t["covers"])
            files = t["files"]
            in_closure = any(path in closures[f] for f in files)
            if names is not None:       # rule 4: only closures whose files mention a changed name
                in_closure = in_closure and any(_mentions(root, cf, names) for f in files for cf in closures[f])
            if t["layer"] == "journey":
                if by_cov:
                    add(tid, f"covers {path}")
                    hit = True
                elif in_closure:
                    if journey_rule == "spec":
                        add(tid, f"import closure contains {path}")
                    add("journey.first_play", f"a journey's import closure contains {path}")
                    hit = True
            elif by_cov or in_closure:
                add(tid, f"{'covers' if by_cov else 'import closure contains'} {path}")
                hit = True
        for f, ids in pl["unit_owned"].items():
            if path == f or path in closures.get(f, ()) or _match(path, FILE_COVERS.get(Path(f).name, ())):
                if names is None or any(_mentions(root, cf, names) for cf in closures.get(f, ())):
                    hit = True                   # owned by unit-layer checks: the unit layer runs anyway
        return hit

    for p in paths:
        if _match(p, FULL_TRIGGERS):
            full(f"FULL-trigger path {p}")
            continue
        if _match(p, NO_IMPACT):
            out["unit_only"].append(p)
            continue
        if p in CONST_FILES:
            old = old_source(p) if old_source else None
            if new_source:
                new = new_source(p)
            else:
                try:
                    new = (root / p).read_text()
                except OSError:
                    new = None
            names = const_change(old, new) if new is not None else None
            if names is None:
                full(f"{p}: not a constants-only change (rule 4)")
                continue
            if not owners_of(p, names):
                out["unit_only"].append(p)       # constants nobody's closure mentions
            continue
        if not owners_of(p):
            full(f"unmapped path {p}")
    if any(t in sel and tests[t]["layer"] == "journey" for t in tests) and "journey.first_play" not in sel:
        add("journey.first_play", "a journey is selected")
    out["duration_s"] = round(time.perf_counter() - t0, 3)
    return out


_MENTIONS: dict = {}


def _mentions(root: Path, rel: str, names: set[str]) -> bool:
    key = (rel, frozenset(names))
    if key not in _MENTIONS:
        try:
            src = (root / rel).read_text()
        except OSError:
            src = ""
        _MENTIONS[key] = any(re.search(rf"\b{re.escape(n)}\b", src) for n in names)
    return _MENTIONS[key]


def _repo_files(root: Path) -> list[str]:
    r = subprocess.run(["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard"],
                       capture_output=True, text=True)
    return r.stdout.splitlines() if r.returncode == 0 else []


# ---------------------------------------------------------------- replay (task 1.3; `hs select --replay N`)

# Projected seconds per acceptance test after migration (design 9.3 / 9.4). Tests not listed are priced with their
# last full run's duration (the sum over the checks they replace).
PROJECTED_S = {
    "play.loop_clean_exit": 55, "play.interrupt_clean_exit": 35, "play.client_crash_cleanup": 33,
    "play.player_account": 47, "backups.on_quit": 71, "backups.failure_nonblocking": 39,
    "lifecycle.persist": 25, "core.login_logout_race": 5.7, "login.continue_context": 1.0,
    "client.login_text_clean": 31.8, "era.toggle_roundtrip": 34, "era.scope_guard": 30,
    "pause.freeze_matrix": 55, "pause.respawn_frozen": 50, "pause.refused_on_taxi": 11.8,
    "pause.logout_unpauses": 22, "pause.disabled_noop": 45, "pause.disconnect_unpauses": 15,
    "saves.scope": 50, "saves.refused_in_combat": 12, "saves.load_refusals": 8, "savesctl.hygiene": 25,
    "backups.kill_safe": 45, "backups.restore_roundtrip": 70, "backups.restore_refusals": 15,
    "app.loading_stages_warm": 25, "client.config_gate": 30, "client.real_client_refused": 3,
    "lifecycle.isolated": 3,
    "journey.first_play": 115, "journey.pause": 55, "journey.saves": 110, "journey.login_recovery": 85,
    "journey.stock_client": 30, "journey.app_desktop": 180,
}
# Fixed shared-segment overhead when any shared-server test is selected (design 9.4): start 24.3 + smoke 15 +
# down 5.9 + run guards 4. The smoke checks themselves are inside the 15.
SHARED_OVERHEAD_S = 49.2
SMOKE = ("gm.", "sql.", "fixture.", "client.realmlist", "client.protocol_", "build.playerbots_active",
         "lifecycle.idempotent", "lifecycle.isolated", "lifecycle.up", "lifecycle.down", "play.player_account_untouched")
COLD = ("play.loop_clean_exit", "play.interrupt_clean_exit", "play.client_crash_cleanup", "play.startup_failure",
        "lifecycle.failfast", "app.startup_failure", "play.player_account", "backups.on_quit",
        "backups.failure_nonblocking", "firstlaunch.client_refusals", "release.pins", "harness.prefix_scope")
# character-scoped (2-worker pool) tests, design 9.3
POOLED = ("saves.manual", "saves.refused_in_combat", "saves.autosave_triggers", "saves.bots_skip", "saves.roundtrip",
          "saves.item_ids_fresh", "saves.inworld_load", "saves.undo", "saves.load_atomic", "saves.load_refusals",
          "comp.add_remove", "comp.follows", "comp.assists", "comp.quest_shared", "era.sql_deterministic",
          "era.default_on", "era.xp_curve", "era.quest_xp", "era.elites", "era.mounts", "login.continue_context",
          "client.login_text_clean", "savesctl.test_actions_refused")


def price(tests: dict, selected: set, last: dict, pool: bool = True) -> dict:
    """Seconds of integration, journeys and gate for a selection (FULL = every test), from projections and the
    last full run's per-check durations."""
    def secs(tid):
        if tid in PROJECTED_S:
            return PROJECTED_S[tid]
        return sum(last.get(c, 0.0) for c in tests[tid]["from"])
    integ = jour = gate = 0.0
    pooled = 0.0
    shared = False
    for tid in selected:
        t = tests[tid]
        s = secs(tid)
        if t["layer"] == "journey":
            jour += s
        elif t["layer"] == "gate":
            gate += s
        else:
            if tid.startswith(SMOKE):
                shared = True
                continue                          # inside SHARED_OVERHEAD_S
            if tid not in COLD:
                shared = True
            if pool and tid in POOLED:
                pooled += s
            else:
                integ += s
    integ += pooled / 2 if pool else 0
    if shared:
        integ += SHARED_OVERHEAD_S
    return {"integration_s": round(integ, 1), "journeys_s": round(jour, 1), "gate_s": round(gate, 1)}


def merged_prs(n: int, root: Path | None = None, upto: str | None = None) -> list[dict]:
    """The last n merged PRs on origin/main (first-parent merge commits "Merge pull request #N"), oldest first."""
    root = Path(root or P.ROOT)
    r = subprocess.run(["git", "-C", str(root), "log", upto or "origin/main", "--merges", "--first-parent",
                        "--format=%H %s"], capture_output=True, text=True, check=True)
    out = []
    for line in r.stdout.splitlines():
        sha, subj = line.split(" ", 1)
        m = re.match(r"Merge pull request #(\d+) from \S+/(\S+)", subj)
        if m:
            out.append({"sha": sha, "pr": int(m.group(1)), "branch": m.group(2)})
        if len(out) == n:
            break
    return list(reversed(out))


def replay(prs: list[dict], unit_s: float, root: Path | None = None, pool: bool = True) -> dict:
    """Each PR's selection under both FULL rules and both journey rules, priced; plus the means."""
    import json
    root = Path(root or P.ROOT)
    last = {}
    try:
        doc = json.loads((P.OUT / "last-full-run" / "results.json").read_text())
        last = {c["id"]: float(c.get("duration_s") or 0.0) for c in doc["checks"]}
        last_meta = {k: doc["meta"].get(k) for k in ("started", "repo_sha")}
    except (OSError, ValueError, KeyError):
        last_meta = None
    pl = plan()
    tests = pl["tests"]
    closures = _closures(pl, root)
    everything = set(tests)
    integ_all = {t for t in tests if tests[t]["layer"] == "integration"}
    rows = []
    for pr in prs:
        sha = pr["sha"]
        d = subprocess.run(["git", "-C", str(root), "diff", "--name-only", "--no-renames", f"{sha}^1", sha],
                           capture_output=True, text=True)
        paths = d.stdout.splitlines() if d.returncode == 0 else None

        def show(rev):
            def f(p):
                r = subprocess.run(["git", "-C", str(root), "show", f"{rev}:{p}"], capture_output=True, text=True)
                return r.stdout if r.returncode == 0 else None
            return f
        row = {"pr": pr["pr"], "branch": pr["branch"], "paths": len(paths or [])}
        for rule in ("spec", "design"):
            s = select(paths, root=root, pl=pl, closures=closures, old_source=show(f"{sha}^1"),
                       new_source=show(sha), journey_rule=rule)
            sel = set(s["selected"])
            if s["full"]:
                strict = everything
                # option 2 (proposal open question 1 (b)): complete integration + first_play + the journeys whose
                # covers match the PR's paths; gate checks only as selected by the rules
                cov_j = {t for t in tests if tests[t]["layer"] == "journey"
                         and any(_match(p, tests[t]["covers"]) for p in (paths or []))}
                gates = {t for t in sel if tests[t]["layer"] == "gate"}
                opt2 = integ_all | {"journey.first_play"} | cov_j | gates
                strict_sel = (everything - {t for t in tests if tests[t]["layer"] == "gate"}) | gates
            else:
                strict_sel = opt2 = sel
            ps, po = price(tests, strict_sel, last, pool), price(tests, opt2, last, pool)
            row[rule] = {"full": s["full"], "full_reasons": s["full_reasons"][:4], "selected": sorted(sel),
                         "n_selected": len(sel), "selection_s": s["duration_s"],
                         "strict": ps, "option2": po,
                         "strict_min": round((unit_s + s["duration_s"] + ps["integration_s"] + ps["journeys_s"]) / 60, 1),
                         "option2_min": round((unit_s + s["duration_s"] + po["integration_s"] + po["journeys_s"]) / 60, 1),
                         "strict_min_with_gate": round((unit_s + s["duration_s"] + sum(ps.values())) / 60, 1),
                         "option2_min_with_gate": round((unit_s + s["duration_s"] + sum(po.values())) / 60, 1)}
        rows.append(row)
    n = len(rows) or 1
    means = {f"{rule}_{k}": round(sum(r[rule][k] for r in rows) / n, 1) for rule in ("spec", "design")
             for k in ("strict_min", "option2_min", "strict_min_with_gate", "option2_min_with_gate")}
    means |= {f"{rule}_full_share": round(sum(r[rule]["full"] for r in rows) / n, 2) for rule in ("spec", "design")}
    complete = price(tests, everything - {t for t in tests if tests[t]["layer"] == "gate"}, last, pool)
    return {"rows": rows, "means": means, "unit_s": unit_s, "pool": pool, "priced_with": last_meta,
            "complete_suite": complete,
            "max_selection_s": max((r[x]["selection_s"] for r in rows for x in ("spec", "design")), default=0)}
