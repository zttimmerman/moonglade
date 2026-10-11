"""Impact selection for `hs run --tier pr` (harness-reporting "Impact selection is fail-safe" and "Every product path
has an owner"; openspec/changes/test-pyramid design 13, prototyped in 1.5 as ownership.py).

Selection by declared ownership. Each integration, journey and gate check declares `covers` (registry: per check, or
its file's COVERS): repo paths or globs, and config sections as `<conf path>#<Section>` / fork.lock keys as
`fork.lock#<key glob>`. A changed path selects:
1. FULL for a foundation: patches/core/**, patches/playerbots/**, a core or playerbots pin key in fork.lock,
   harness/hslib/build.py, a CMake file;
2. nothing beyond the unit layer for openspec/**, docs/**, *.md, harness/tests/unit/**, harness/active-changes
   (every change edits it);
3. the tests whose covers name it (a module config selects by the sections whose keys changed; fork.lock by the keys
   that changed; a harness module imported only by declared non-shared modules selects their owners);
4. the smoke set (journey.first_play + the cheapest integration test of each area) when nothing covers it, when it
   is shared infrastructure (a module six or more acceptance check files import directly, plus probe/**, stack.py,
   lanes.py, lanestack.py, runner.py), or when it is mod_moonglade_loader.cpp.
A git failure, an unparseable source or a stale covers entry selects FULL. Everything is recorded in meta.selection.
Gate checks are selected by the same rules but never run in a pr run: they are listed as owed (gate_owed), with the
path that selected each (harness-reporting "Gate-layer checks").
"""
from __future__ import annotations

import collections
import difflib
import fnmatch
import json
import re
import subprocess
import time
from pathlib import Path

from . import paths as P

FULL_TRIGGERS = ("patches/core/**", "patches/playerbots/**", "harness/hslib/build.py", "CMakeLists.txt",
                 "**/CMakeLists.txt", "*.cmake", "**/*.cmake")
FOUNDATION_KEYS = ("core_repo", "core_branch", "core_sha", "playerbots_repo", "playerbots_branch", "playerbots_sha")
NO_IMPACT = ("openspec/**", "docs/**", "*.md", "**/*.md", "harness/tests/unit/**", "harness/active-changes")
SHARED_MIN = 6
SHARED_ALWAYS = ("harness/hslib/probe/**", "harness/hslib/stack.py", "harness/hslib/lanes.py",
                 "harness/hslib/lanestack.py", "harness/hslib/runner.py")
SMOKE_PATHS = ("modules/mod-moonglade/src/mod_moonglade_loader.cpp",)
# Product paths that must have an owner (report.ownership)
PRODUCT = ("modules/**", "client-mods/**", "harness/app/**", "play", "bootstrap", "tools/release/**")
PR_RUNS = P.OUT / "pr-runs.jsonl"
SMOKE_JOURNEY = "journey.first_play"


def match(path: str, globs) -> bool:
    for g in globs:
        if fnmatch.fnmatchcase(path, g):
            return True
        if g.endswith("/**") and path.startswith(g[:-2]):
            return True
        if g.startswith("**/") and fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], g[3:]):
            return True
    return False


def _split(entry: str) -> tuple[str, str | None]:
    path, _, sec = entry.partition("#")
    return path, (sec or None)


# ---------------------------------------------------------------- the ownership map

class Map:
    """The tree's ownership map: acceptance tests and their covers, the shared infrastructure, the import graph."""

    def __init__(self, reg: dict, root: Path | None = None, files: list[str] | None = None):
        self.root = Path(root or P.ROOT)
        self.tests = {i: c for i, c in reg.items() if c.layer in ("integration", "journey", "gate")}
        self.files = files if files is not None else tracked_files(self.root)
        self.errors: list[str] = []
        self.importers: dict = collections.defaultdict(set)     # hslib module -> modules importing it directly
        direct: dict = {}
        for f in self.files:
            if f.startswith("harness/hslib/") and f.endswith(".py"):
                try:
                    direct[f] = _direct_imports(self.root, f)
                except SyntaxError as e:
                    self.errors.append(f"unparseable source {f}: {e}")
                    direct[f] = set()
                for m in direct[f]:
                    self.importers[m].add(f)
        check_files = {_source_file(c, self.root) for c in self.tests.values()}
        cnt = collections.Counter(m for f in check_files if f in direct for m in direct[f])
        self.shared = sorted({m for m, n in cnt.items() if n >= SHARED_MIN}
                             | {f for f in self.files if match(f, SHARED_ALWAYS)})

    def stale(self) -> list[str]:
        """Covers entries that match no tracked file (or a config section/key the file doesn't have)."""
        out = []
        texts: dict = {}
        for tid, c in sorted(self.tests.items()):
            for e in c.covers:
                path, sec = _split(e)
                hits = [f for f in self.files if match(f, (path,))]
                if not hits:
                    out.append(f"{tid}: {e!r} matches no file")
                    continue
                if sec and len(hits) == 1:
                    t = texts.setdefault(hits[0], _read(self.root / hits[0]))
                    keys = _keys(t)
                    if not any(fnmatch.fnmatchcase(k, sec if any(ch in sec for ch in "*?[") else sec + "*")
                               for k in keys):
                        out.append(f"{tid}: {e!r} names a section or key the file doesn't have")
        return out

    def owners(self, path: str, keys: set[str] | None = None) -> set[str]:
        """Tests whose covers name `path` (with `keys`: only entries for those config sections/keys)."""
        out = set()
        for tid, c in self.tests.items():
            for e in c.covers:
                p, sec = _split(e)
                if not match(path, (p,)):
                    continue
                if sec is None:
                    if keys is None or not path.endswith((".conf.dist", "fork.lock")):
                        out.add(tid)
                elif keys is not None:
                    pat = sec if any(ch in sec for ch in "*?[") else sec + "*"
                    if any(fnmatch.fnmatchcase(k, pat) for k in keys):
                        out.add(tid)
        return out

    def owners_via_importers(self, path: str, seen=None) -> set[str]:
        """A harness module nobody declares, imported only by non-shared modules: their owners (transitively)."""
        seen = seen or set()
        out = set()
        for m in sorted(self.importers.get(path, ())):
            if m in seen or m in self.shared:
                continue
            seen.add(m)
            out |= self.owners(m) or self.owners_via_importers(m, seen)
        return out

    def smoke_set(self, price) -> list[str]:
        """journey.first_play + the cheapest integration test of each area (check id prefix)."""
        best: dict = {}
        for tid, c in self.tests.items():
            if c.layer != "integration":
                continue
            area = tid.split(".", 1)[0]
            s = price(tid)
            if area not in best or (s, tid) < best[area]:
                best[area] = (s, tid)
        out = sorted(v[1] for v in best.values())
        return ([SMOKE_JOURNEY] if SMOKE_JOURNEY in self.tests else []) + out


def _read(p: Path) -> str:
    try:
        return p.read_text(errors="replace")
    except OSError:
        return ""


def _keys(text: str) -> set[str]:
    """Config keys of a .conf.dist (commented or not) or fork.lock."""
    return {m.group(1) for m in re.finditer(r"^\s*#?\s*([A-Za-z][\w.]*)\s*=", text, re.M)}


def changed_keys(old: str | None, new: str | None) -> set[str] | None:
    """The config keys whose lines differ (None when unknown: fail-safe callers treat it as unowned)."""
    if old is None or new is None:
        return None
    keys = set()
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0):
        if line.startswith(("+++", "---", "@@")):
            continue
        m = re.match(r"^\s*#?\s*([A-Za-z][\w.]*)\s*=", line[1:])
        if m:
            keys.add(m.group(1))
    return keys


def _source_file(c, root: Path) -> str:
    import inspect
    try:
        return str(Path(inspect.getsourcefile(c.fn)).resolve().relative_to(root.resolve()))
    except (TypeError, ValueError):
        return ""


def _direct_imports(root: Path, rel: str) -> set[str]:
    from .runner import _import_targets, _module_file
    p = root / rel
    parts = p.relative_to(root / "harness").with_suffix("").parts
    out = set()
    for pp, name in _import_targets(p, list(parts[:-1])):
        m = _module_file(root, pp, name)
        if m is not None and m != p:
            out.add(str(m.relative_to(root)))
    return out


def tracked_files(root: Path) -> list[str]:
    r = subprocess.run(["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard"],
                       capture_output=True, text=True)
    return sorted(f for f in set(r.stdout.splitlines()) if (root / f).exists()) if r.returncode == 0 else []


# ---------------------------------------------------------------- selection

def select(paths: list[str] | None, m: Map, price, old_source=None, new_source=None) -> dict:
    """The selection for changed `paths` (None = git failed). `old_source(path)` / `new_source(path)` give a file's
    text before and after the change (config sections, fork.lock keys)."""
    t0 = time.perf_counter()
    out = {"full": False, "full_reasons": [], "selected": {}, "unit_only": [], "smoke_paths": [], "smoke_set": [],
           "paths": paths}

    def full(why):
        out["full"] = True
        out["full_reasons"].append(why)
    if paths is None:
        full("git failed: the changed paths are unknown")
    for e in m.errors:
        full(e)
    for e in m.stale():
        full(f"stale covers entry: {e}")
    sel = out["selected"]
    for p in paths or []:
        if p == "fork.lock":
            keys = changed_keys(old_source(p) if old_source else None, new_source(p) if new_source else _read(m.root / p))
            if keys is None or keys & set(FOUNDATION_KEYS):
                full(f"fork.lock: {'a core or playerbots pin changed' if keys else 'its change is unknown'}")
                continue
            own = m.owners(p, keys)
            for t in own:
                sel.setdefault(t, []).append(f"covers fork.lock {sorted(keys)}")
            if not own and keys:
                out["smoke_paths"].append(f"fork.lock {sorted(keys)}")
            continue
        if match(p, FULL_TRIGGERS):
            full(f"foundation path {p}")
            continue
        if match(p, NO_IMPACT):
            out["unit_only"].append(p)
            continue
        if p in SMOKE_PATHS or p in m.shared:
            out["smoke_paths"].append(p)
            for t in m.owners(p):              # a test that declares shared infrastructure runs too
                sel.setdefault(t, []).append(f"covers {p} (shared)")
            continue
        if p.endswith(".conf.dist"):
            keys = changed_keys(old_source(p) if old_source else None, new_source(p) if new_source else _read(m.root / p))
            own = m.owners(p, keys) if keys else set()
            if keys:
                sec_owned = {k for k in keys if m.owners(p, {k})}
                if sec_owned != keys:
                    out["smoke_paths"].append(f"{p} {sorted(keys - sec_owned)}")
            elif keys is None:
                out["smoke_paths"].append(p)
            for t in own:
                sel.setdefault(t, []).append(f"{p}: section of changed key(s) {sorted(keys)}")
            continue
        own = m.owners(p)
        if not own and p.startswith("harness/hslib/") and p.endswith(".py"):
            own = m.owners_via_importers(p)
            reason = f"imported by a module it declares ({p})"
        else:
            reason = f"covers {p}"
        if not own:
            out["smoke_paths"].append(p)
            continue
        for t in own:
            sel.setdefault(t, []).append(reason)
    if out["smoke_paths"] and not out["full"]:
        out["smoke_set"] = m.smoke_set(price)
        for t in out["smoke_set"]:
            sel.setdefault(t, []).append("smoke set: " + ", ".join(out["smoke_paths"][:3]))
    out["gate_owed"] = gate_owed(sel, m.tests)
    out["duration_s"] = round(time.perf_counter() - t0, 3)
    return out


def gate_owed(selected: dict, tests: dict) -> dict:
    """The selected gate checks with the reasons (the selecting paths) that selected them: a pr run never runs them
    and lists them as owed (harness-reporting "Gate-layer checks")."""
    return {t: list(r) for t, r in sorted(selected.items()) if t in tests and tests[t].layer == "gate"}


def pr_chosen(sel: dict, reg: dict) -> set:
    """The checks a pr run executes beyond the unit layer and the run guards: on FULL every integration and journey
    check, otherwise the selected ones; never a gate check."""
    ids = set(reg) if sel["full"] else set(sel["selected"])
    return {i for i in ids if i in reg and reg[i].layer != "gate"}


def last_full_times() -> dict:
    try:
        doc = json.loads((P.OUT / "last-full-run" / "results.json").read_text())
        return {c["id"]: float(c.get("duration_s") or 0.0) for c in doc["checks"]}
    except (OSError, ValueError, KeyError):
        return {}


def baseline_times() -> dict:
    try:
        b = json.loads((P.HARNESS / "timing-baseline.json").read_text())
        return {i: float(v["s"]) for i, v in b["checks"].items()}
    except (OSError, ValueError, KeyError):
        return {}


def pricer(reg: dict):
    last, base = last_full_times(), baseline_times()

    def price(tid):
        if tid in last:
            return last[tid]
        if tid in base:
            return base[tid]
        c = reg.get(tid)
        return float(c.budget_s) if c is not None and c.budget_s else 60.0
    return price


def select_for_run(reg: dict, root: Path | None = None) -> dict:
    """This checkout's selection (meta.selection)."""
    from .runner import changed_paths
    t0 = time.perf_counter()
    root = Path(root or P.ROOT)
    c = changed_paths(root)
    base = c["base"]

    def old(p):
        if not base:
            return None
        r = subprocess.run(["git", "-C", str(root), "show", f"{base}:{p}"], capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else ""
    m = Map(reg, root)
    s = select(c["paths"], m, pricer(reg), old_source=old)
    s |= {"base_ref": c["base_ref"], "base": base, "git_error": c["error"], "shared": m.shared,
          "duration_s": round(time.perf_counter() - t0, 3)}
    return s


# ---------------------------------------------------------------- ownership (report.ownership)

def unowned(m: Map) -> list[str]:
    """Tracked product paths no covers names and no FULL / no-impact / smoke rule owns."""
    out = []
    for f in m.files:
        if not match(f, PRODUCT) or match(f, FULL_TRIGGERS) or match(f, NO_IMPACT) or f in SMOKE_PATHS \
                or f.rsplit("/", 1)[-1] == ".gitkeep":
            continue
        if f.endswith(".conf.dist"):
            if any(_split(e)[0] == f or match(f, (_split(e)[0],)) for c in m.tests.values() for e in c.covers):
                continue
            out.append(f)
            continue
        if not m.owners(f):
            out.append(f)
    return out


# ---------------------------------------------------------------- pr run history and the replay

def record_pr_run(doc: dict, path: Path | None = None) -> dict:
    """Append this pr run to harness/out/pr-runs.jsonl (report.pr_average)."""
    m = doc["meta"]
    L = m.get("layers") or {}
    s = m.get("selection") or {}
    rec = {"sha": m.get("repo_sha"), "started": m.get("started"),
           "acceptance_s": (L.get("acceptance") or {}).get("wall_s"), "full": bool(s.get("full")),
           "paths": (s.get("paths") or [])[:200], "ok": m.get("ok")}
    path = path or PR_RUNS
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def merged_prs(n: int, root: Path | None = None, upto: str = "origin/main") -> list[dict]:
    root = Path(root or P.ROOT)
    r = subprocess.run(["git", "-C", str(root), "log", upto, "--merges", "--first-parent", "--format=%H %s"],
                       capture_output=True, text=True)
    out = []
    for line in r.stdout.splitlines():
        sha, _, subj = line.partition(" ")
        mm = re.match(r"Merge pull request #(\d+) from \S+/(\S+)", subj)
        if mm:
            out.append({"sha": sha, "pr": int(mm.group(1)), "branch": mm.group(2)})
        if len(out) == n:
            break
    return list(reversed(out))


def replay(n: int = 15, reg: dict | None = None, root: Path | None = None) -> dict:
    """The selector over the last n merged PRs, priced with the last full run's per-check times (the FULL price is
    every integration and journey check). Minutes are integration + journeys."""
    from .registry import load_all
    reg = reg if reg is not None else load_all()
    root = Path(root or P.ROOT)
    price = pricer(reg)
    m = Map(reg, root)
    acc = [i for i, c in reg.items() if c.layer in ("integration", "journey")]
    full_s = sum(price(i) for i in acc)
    rows = []
    for pr in merged_prs(n, root):
        sha = pr["sha"]
        d = subprocess.run(["git", "-C", str(root), "diff", "--name-only", "--no-renames", f"{sha}^1", sha],
                           capture_output=True, text=True)
        paths = d.stdout.splitlines() if d.returncode == 0 else None

        def show(rev):
            def f(p):
                r = subprocess.run(["git", "-C", str(root), "show", f"{rev}:{p}"], capture_output=True, text=True)
                return r.stdout if r.returncode == 0 else ""
            return f
        s = select(paths, m, price, show(f"{sha}^1"), show(sha))
        sel = [i for i in s["selected"] if reg[i].layer in ("integration", "journey")] if not s["full"] else acc
        secs = sum(price(i) for i in sel)
        rows.append({"pr": pr["pr"], "branch": pr["branch"], "full": s["full"], "full_reasons": s["full_reasons"][:3],
                     "selected": len(sel), "smoke": bool(s["smoke_paths"]), "minutes": round(secs / 60, 1),
                     "selection_s": s["duration_s"]})
    mean = round(sum(r["minutes"] for r in rows) / len(rows), 1) if rows else None
    return {"prs": rows, "mean_min": mean, "full_min": round(full_s / 60, 1),
            "full_share": round(sum(r["full"] for r in rows) / len(rows), 2) if rows else None,
            "max_selection_s": max((r["selection_s"] for r in rows), default=None)}
