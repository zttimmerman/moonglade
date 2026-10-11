"""Ownership-based impact selection, PROTOTYPE (openspec/changes/test-pyramid design 13; task 1.5).

Selection by declared ownership instead of import closure:
1. FULL only for true foundations: patches/core/**, patches/playerbots/**, a change to fork.lock's core_* or
   playerbots_* pins, and the build system (harness/hslib/build.py, any CMakeLists.txt or *.cmake).
2. No-impact paths (openspec/**, docs/**, *.md, harness/tests/unit/**): the unit layer only.
3. Otherwise the acceptance tests (integration clusters, journeys, gate checks) whose `covers` declare the path.
4. A path nobody declares selects the SMOKE set: journey.first_play plus the cheapest integration test of each area.
   (`report.ownership`, a unit check in task 2.x, will fail a product path without an owner; harness-library paths
   fall to the smoke set by design.)

Until task 2.1 puts `covers=` on each check, `generate()` derives a first-cut map; the rule, so it can be reviewed:
- acceptance tests and layers come from selector.plan() (design.md appendix A); the m4-dungeon-scaling checks
  (scale.*, lock.*) form one `dng.session` cluster, except the boss fight scale.solo_final_boss (gate);
- product paths: selector.FILE_COVERS of the test's check file, and design 10's covers for journeys;
- harness libraries: the check file itself, plus every hslib module it imports DIRECTLY (one hop, not the closure),
  except shared infrastructure: modules that at least SHARED_MIN of the acceptance check files import directly
  (registry, paths, stack, soap, play, db, launch, ...). A shared module is still declared by the check file named
  after it (play.py by play_checks.py, stack.py by none). Shared modules fall to the smoke set.
"""
from __future__ import annotations

import collections
import fnmatch
import re
import subprocess
import time
from pathlib import Path

from . import paths as P
from . import selector as S

SHARED_MIN = 6
# also shared infrastructure, whatever the import counts (design 13 rule 2: harness, probe, stack and lane code are
# tested by unit tests and the smoke set)
SHARED_ALWAYS = ("harness/hslib/probe/**", "harness/hslib/stack.py", "harness/hslib/lanes.py",
                 "harness/hslib/lanestack.py", "harness/hslib/runner.py", "harness/hslib/registry.py")
MOD = "modules/mod-moonglade/"
# product paths that every module feature passes through: owned by the smoke set, not by every area
SMOKE_PRODUCT = (MOD + "src/mod_moonglade_loader.cpp",)
# config files are owned by section: a changed `<Section>.<key>` line selects that section's area
CONF_SECTIONS = {
    MOD + "conf/mod_moonglade.conf.dist": {"Moonglade.Saves": "saves", "Moonglade.Pause": "pause",
                                           "Moonglade.Dungeons": "dng", "Moonglade.QuietServerText": "client",
                                           "CharacterDatabase.WorkerThreads": "saves"},
    "modules/mod-era-profile/conf/mod_era_profile.conf.dist": {"EraProfile": "era"},
}
FULL_TRIGGERS = ("patches/core/**", "patches/playerbots/**", "harness/hslib/build.py", "**/CMakeLists.txt",
                 "CMakeLists.txt", "*.cmake", "**/*.cmake")
FOUNDATION_PIN_KEYS = ("core_repo", "core_branch", "core_sha", "playerbots_repo", "playerbots_branch", "playerbots_sha")
NO_IMPACT = S.NO_IMPACT
GATE_ONLY = ("scale.solo_final_boss",)


def area_of(tid: str) -> str:
    if tid.startswith(("scale.", "lock.")) and tid not in GATE_ONLY:
        return "dng"
    return tid.split(".", 1)[0]


def _match(path: str, globs) -> bool:
    return any(fnmatch.fnmatchcase(path, g) or (g.endswith("/**") and path.startswith(g[:-2])) for g in globs)


def _direct_imports(rel: str, root: Path) -> set[str]:
    from .runner import _import_targets, _module_file
    p = root / rel
    parts = p.relative_to(root / "harness").with_suffix("").parts
    out = set()
    for pp, name in _import_targets(p, list(parts[:-1])):
        m = _module_file(root, pp, name)
        if m is not None and m != p:
            out.add(str(m.relative_to(root)))
    return out


def generate(root: Path | None = None) -> dict:
    """{"tests": {test id: {layer, area, from, covers}}, "shared": [...], "derivation": {...}}."""
    root = Path(root or P.ROOT)
    pl = S.plan()
    tests = {}
    for tid, t in pl["tests"].items():
        layer = "gate" if tid in GATE_ONLY else t["layer"]
        tests[tid] = {"layer": layer, "area": area_of(tid), "from": t["from"], "files": set(t["files"]),
                      "product": tuple(t["covers"])}
    files = sorted({f for t in tests.values() for f in t["files"]})
    direct = {f: _direct_imports(f, root) for f in files}
    cnt = collections.Counter(m for v in direct.values() for m in v)
    shared = sorted(m for m, n in cnt.items() if n >= SHARED_MIN or _match(m, SHARED_ALWAYS))
    for t in tests.values():
        libs = set(t["files"])
        for f in t["files"]:
            stem = Path(f).stem.removesuffix("_checks")
            for m in direct[f]:
                if m not in shared or Path(m).stem == stem:
                    libs.add(m)
        t["libs"] = tuple(sorted(libs))
        t["product"] = tuple(g for g in t["product"] if g not in SMOKE_PRODUCT and g not in CONF_SECTIONS
                             and g != MOD + "conf/*")
        t["covers"] = tuple(t["product"]) + t["libs"]
    return {"tests": tests, "shared": shared,
            "derivation": {"check_files": len(files), "shared_min": SHARED_MIN,
                           "shared_counts": {m: cnt[m] for m in shared}}}


def smoke(own: dict, price_s) -> list[str]:
    """journey.first_play + the cheapest integration test of each area."""
    best: dict = {}
    for tid, t in own["tests"].items():
        if t["layer"] != "integration" or tid.startswith(S.SMOKE):
            continue
        s = price_s(tid)
        if t["area"] not in best or s < best[t["area"]][0]:
            best[t["area"]] = (s, tid)
    return ["journey.first_play"] + sorted(v[1] for v in best.values())


def fork_lock_foundation(old: str | None, new: str | None) -> bool:
    """Did fork.lock's core or bots pins change? (unknown text counts as changed: fail-safe)"""
    if old is None or new is None:
        return True
    def pins(text):
        d = {}
        for line in text.splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
        return {k: d.get(k) for k in FOUNDATION_PIN_KEYS}
    return pins(old) != pins(new)


def conf_areas(path: str, old: str | None, new: str | None) -> set[str] | None:
    """The areas whose config section a change touches (by the changed lines' keys, commented or not); None when a
    changed line names no known section (the smoke set)."""
    import difflib
    if old is None or new is None:
        return None
    secs = CONF_SECTIONS[path]
    areas = set()
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0):
        if line.startswith(("+++", "---", "@@")) or not line[1:].strip():
            continue
        body = line[1:].lstrip("#").strip()
        key = re.match(r"([A-Za-z]+(?:\.[A-Za-z]+)*)\s*=", body)
        if not key:
            continue                     # prose comment lines document a key; they don't change behaviour
        hit = next((a for sname, a in secs.items() if key.group(1).startswith(sname)), None)
        if hit is None:
            return None
        areas.add(hit)
    return areas


def select(paths, own: dict, smoke_ids: list[str], old_source=None, new_source=None) -> dict:
    t0 = time.perf_counter()
    out = {"full": False, "full_reasons": [], "selected": {}, "smoke_paths": [], "unit_only": []}
    if paths is None:
        out["full"] = True
        out["full_reasons"].append("git failed: the changed paths are unknown")
        out["duration_s"] = round(time.perf_counter() - t0, 3)
        return out
    sel = out["selected"]
    for p in paths:
        if p == "fork.lock":
            if fork_lock_foundation(old_source(p) if old_source else None, new_source(p) if new_source else None):
                out["full"] = True
                out["full_reasons"].append("fork.lock: a core or playerbots pin changed")
            else:
                for tid, t in own["tests"].items():          # the other pins: their declared owners
                    if "fork.lock" in t["covers"]:
                        sel.setdefault(tid, []).append("covers fork.lock")
            continue
        if _match(p, FULL_TRIGGERS):
            out["full"] = True
            out["full_reasons"].append(f"foundation path {p}")
            continue
        if p in CONF_SECTIONS:
            areas = conf_areas(p, old_source(p) if old_source else None, new_source(p) if new_source else None)
            if areas is None:
                out["smoke_paths"].append(p)
            else:
                for tid, t in own["tests"].items():
                    if t["area"] in areas and t["layer"] != "gate":
                        sel.setdefault(tid, []).append(f"{p}: section of {t['area']} changed")
                if "pause" in areas or "saves" in areas:      # their journeys assert the switch's effect
                    for j in ("journey.pause",) * ("pause" in areas) + ("journey.saves",) * ("saves" in areas):
                        sel.setdefault(j, []).append(f"{p}: section changed")
            continue
        if _match(p, NO_IMPACT):
            out["unit_only"].append(p)
            continue
        hit = False
        for tid, t in own["tests"].items():
            if _match(p, t["covers"]):
                sel.setdefault(tid, []).append(f"covers {p}")
                hit = True
        if not hit:
            out["smoke_paths"].append(p)
    if out["smoke_paths"]:
        for tid in smoke_ids:
            sel.setdefault(tid, []).append("smoke set (undeclared path)")
    if any(own["tests"][t]["layer"] == "journey" for t in sel if t in own["tests"]):
        sel.setdefault("journey.first_play", []).append("a journey is selected")
    out["duration_s"] = round(time.perf_counter() - t0, 3)
    return out


# ---------------------------------------------------------------- pricing and replay

def pricer(own: dict, last: dict, integration_scale: float = 1.0, measured: dict | None = None):
    """Seconds per test: measured (task 1.6) > design 9.3/9.4 projection > last full run; integration tests times
    `integration_scale` (the redesign's clustering, 1.0 = no gain)."""
    measured = measured or {}

    def secs(tid):
        if tid in measured:
            return measured[tid]
        t = own["tests"][tid]
        base = S.PROJECTED_S.get(tid, sum(last.get(c, 0.0) for c in t["from"]))
        return base * integration_scale if t["layer"] == "integration" else base
    return secs


def price(own: dict, selected, secs, pool: bool, measured_areas=()) -> dict:
    integ = jour = gate = 0.0
    shared = False
    pooled = 0.0
    for tid in selected:
        t = own["tests"][tid]
        s = secs(tid)
        if t["layer"] == "journey":
            jour += s
        elif t["layer"] == "gate":
            gate += s
        else:
            if tid.startswith(S.SMOKE):
                shared = True
                continue
            if tid not in S.COLD:
                shared = True
            if pool and tid in S.POOLED and t["area"] not in measured_areas:
                pooled += s
            else:
                integ += s
    integ += pooled * (1 - 0.366)          # the measured pool saving (1.2), not the assumed 50 %
    if shared:
        integ += S.SHARED_OVERHEAD_S
    return {"integration_s": round(integ, 1), "journeys_s": round(jour, 1), "gate_s": round(gate, 1)}


def merged_prs(n: int, root: Path | None = None) -> list[dict]:
    return S.merged_prs(n, root)


def replay(prs: list[dict], unit_s: float, integration_scale: float = 1.0, measured: dict | None = None,
           measured_areas=(), pool: bool = True, root: Path | None = None) -> dict:
    import json
    root = Path(root or P.ROOT)
    doc = json.loads((P.OUT / "last-full-run" / "results.json").read_text())
    last = {c["id"]: float(c.get("duration_s") or 0.0) for c in doc["checks"]}
    t_gen = time.perf_counter()
    own = generate(root)
    secs = pricer(own, last, integration_scale, measured)
    sm = smoke(own, secs)
    gen_s = round(time.perf_counter() - t_gen, 3)
    acceptance = {t for t in own["tests"] if own["tests"][t]["layer"] != "gate"}
    integ_all = {t for t in own["tests"] if own["tests"][t]["layer"] == "integration"}
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
        s = select(paths, own, sm, show(f"{sha}^1"), show(sha))
        sel = set(s["selected"])
        gates = {t for t in sel if own["tests"][t]["layer"] == "gate"}
        if s["full"]:
            strict = acceptance | gates
            cov_j = {t for t in sel if own["tests"][t]["layer"] == "journey"}
            opt2 = integ_all | {"journey.first_play"} | cov_j | gates
        else:
            strict = opt2 = sel
        ps, po = price(own, strict, secs, pool, measured_areas), price(own, opt2, secs, pool, measured_areas)
        mins = lambda p: round((unit_s + s["duration_s"] + p["integration_s"] + p["journeys_s"]) / 60, 1)
        rows.append({"pr": pr["pr"], "branch": pr["branch"], "paths": len(paths or []), "full": s["full"],
                     "full_reasons": s["full_reasons"], "smoke_paths": s["smoke_paths"][:6],
                     "n_smoke_paths": len(s["smoke_paths"]), "selected": sorted(sel), "n_selected": len(sel),
                     "selection_s": s["duration_s"], "strict": ps, "option2": po,
                     "strict_min": mins(ps), "option2_min": mins(po),
                     "gate_min": round(ps["gate_s"] / 60, 1)})
    n = len(rows) or 1
    complete = price(own, acceptance, secs, pool, measured_areas)
    return {"rows": rows, "unit_s": unit_s, "integration_scale": integration_scale, "smoke": sm,
            "generate_s": gen_s, "max_selection_s": max((r["selection_s"] for r in rows), default=0),
            "mean_strict_min": round(sum(r["strict_min"] for r in rows) / n, 1),
            "mean_option2_min": round(sum(r["option2_min"] for r in rows) / n, 1),
            "full_share": round(sum(r["full"] for r in rows) / n, 2),
            "complete_suite": complete,
            "complete_min": round((complete["integration_s"] + complete["journeys_s"]) / 60, 1),
            "smoke_price": price(own, set(sm), secs, pool, measured_areas)}
