"""report.full_run_wall (test-loop-speedups "Full run under 25 minutes", B36).

An unfiltered full-tier run records its wall time and fails at or over LIMIT_S. How the limit applies:
- "strict": the GUI and backups tiers were skipped by design (no GUI-/backups-marked check executed): wall_s itself;
- "adjusted": GUI- or backups-marked checks executed (a GUI or backup path changed): wall_s minus the time of those
  checks. They run in the main lane after the side lane's barrier (sidelane.py), so nothing overlaps them and the
  difference estimates the same run with those tiers skipped by design;
- "serial-reference": a `hs run --serial` run is the reference schedule; its wall time is recorded, not limited.
Statuses: every check's status must equal its status in the other schedule's run of the same commit (a serial run
for a parallel one and vice versa), when such a kept run exists (harness/out/last-serial-full-run/ and
harness/out/last-full-run/); otherwise the record says no reference exists. A dirty tree has no commit to compare.
"""
import json
from pathlib import Path

from . import paths as P

ID = "report.full_run_wall"
LIMIT_S = 1500.0
SERIAL_DIR = P.OUT / "last-serial-full-run"     # every unfiltered full-tier `--serial` run also writes here
# records whose status depends on the schedule's wall time itself (or on this record), so they aren't compared
COMPARE_EXCLUDE = (ID, "report.full_run", "report.timing")


def schedule_mode(meta: dict) -> str | None:
    return (meta.get("schedule") or {}).get("mode")


def is_full_tier_run(meta: dict) -> bool:
    return meta.get("full_run") is True and meta.get("tier") == "full" and not meta.get("filter")


def statuses(doc: dict) -> dict:
    return {r["id"]: r["status"] for r in doc["checks"] if r["id"] not in COMPARE_EXCLUDE}


def status_diffs(doc: dict, ref: dict) -> list[dict]:
    a, b = statuses(doc), statuses(ref)
    return [{"id": i, "this_run": a.get(i, "absent"), "reference": b.get(i, "absent")}
            for i in sorted(set(a) | set(b)) if a.get(i) != b.get(i)]


def tier_time(doc: dict, reg: dict) -> dict:
    """Seconds of executed (non-skip) GUI- and backups-marked checks, per id."""
    out = {}
    for r in doc["checks"]:
        c = reg.get(r["id"])
        if c is not None and (c.gui or c.backups) and r.get("status") != "skip":
            out[r["id"]] = float(r.get("duration_s") or 0.0)
    return out


def load_reference(meta: dict, serial_dir: Path | None = None, last_dir: Path | None = None) -> tuple:
    """(reference doc or None, why): the other schedule's kept full run of the same (clean) commit."""
    sha = meta.get("repo_sha") or ""
    if not sha or sha.endswith("-dirty") or sha == "unknown":
        return None, f"no commit to compare (repo_sha {sha!r})"
    mode = schedule_mode(meta)
    if mode == "serial":
        from .runner import LAST_FULL_RUN
        path, want = Path(last_dir or LAST_FULL_RUN) / "results.json", "parallel"
    else:
        path, want = Path(serial_dir or SERIAL_DIR) / "results.json", "serial"
    try:
        ref = json.loads(path.read_text())
    except FileNotFoundError:
        return None, f"no {want} reference run kept ({path.name} missing in {path.parent.name})"
    except (OSError, ValueError) as e:
        return None, f"{want} reference unreadable: {type(e).__name__}: {e}"
    rm = ref.get("meta") or {}
    if rm.get("repo_sha") != sha:
        return None, f"no {want} reference run for this commit (kept one is {str(rm.get('repo_sha'))[:12]})"
    if schedule_mode(rm) != want or not is_full_tier_run(rm):
        return None, f"kept run of this commit isn't an unfiltered full-tier {want} run"
    return ref, f"{want} run of this commit, started {rm.get('started')}"


def judge(doc: dict, wall_s: float, reg: dict, reference: dict | None = None, ref_why: str = "") -> dict:
    """The report.full_run_wall record for a full-tier run document at wall time `wall_s`."""
    m = doc["meta"]
    mode = schedule_mode(m) or "serial"           # runs before the side lane existed were serial
    tt = tier_time(doc, reg)
    ev = {"wall_s": round(float(wall_s), 1), "limit_s": LIMIT_S, "schedule": m.get("schedule"),
          "repo_sha": m.get("repo_sha"), "started": m.get("started"),
          "gui_tier_skipped_by_design": bool(m.get("gui_tier_skipped")) or not any(reg[i].gui for i in tt),
          "gui_backups_checks_executed_s": tt}
    probs = []
    if not is_full_tier_run(m):
        probs.append("not an unfiltered full-tier run")
    if mode == "serial":
        ev |= {"limit_mode": "serial-reference", "limit_applied": False, "measured_s": ev["wall_s"]}
    elif tt:
        ev |= {"limit_mode": "adjusted", "limit_applied": True,
               "measured_s": round(float(wall_s) - sum(tt.values()), 1),
               "note": "GUI-/backups-marked checks ran (a GUI or backup path changed); limit applied to wall_s minus "
                       "their time (an estimate of the run with those tiers skipped by design)"}
    else:
        ev |= {"limit_mode": "strict", "limit_applied": True, "measured_s": ev["wall_s"]}
    if ev["limit_applied"] and ev["measured_s"] >= LIMIT_S:
        probs.append(f"wall {ev['measured_s']:.1f} s ({ev['limit_mode']}) is not under {LIMIT_S:.0f} s")
    if reference is None:
        ev["status_reference"] = {"exists": False, "why": ref_why or "none given"}
    else:
        d = status_diffs(doc, reference)
        ev["status_reference"] = {"exists": True, "why": ref_why, "mode": schedule_mode(reference["meta"]),
                                  "started": reference["meta"].get("started"),
                                  "compared": len(set(statuses(doc)) | set(statuses(reference))),
                                  "excluded": list(COMPARE_EXCLUDE), "differences": d}
        if d:
            probs.append(f"{len(d)} check status(es) differ from the {ev['status_reference']['mode']} run of this "
                         f"commit: " + ", ".join(f"{x['id']} {x['this_run']}/{x['reference']}" for x in d[:8]))
    return {"id": ID, "capability": "harness-reporting", "status": "fail" if probs else "pass",
            "evidence": ev | {"problems": probs}, "duration_s": 0.0,
            **({"message": "; ".join(probs)[:300]} if probs else {})}


def _stub(wall, mode="parallel", sha="a" * 40, checks=None, gui_skipped=("app.x",), tier="full", filt=()):
    return {"meta": {"full_run": not filt, "tier": tier, "filter": list(filt), "repo_sha": sha, "started": "t",
                     "wall_s": wall, "schedule": {"mode": mode}, "gui_tier_skipped": list(gui_skipped)},
            "checks": checks if checks is not None else [{"id": "a.one", "status": "pass", "duration_s": 10.0},
                                                         {"id": "b.two", "status": "fail", "duration_s": 5.0}]}


def selftest() -> dict:
    """Stubbed cases with known verdicts; a wrong verdict fails the check (so it can't pass by being broken)."""
    from dataclasses import dataclass

    @dataclass
    class C:
        gui: bool = False
        backups: bool = False
    reg = {"a.one": C(), "b.two": C(), "g.ui": C(gui=True), "k.bk": C(backups=True)}
    serial_ref = _stub(2100.0, mode="serial")
    differ = _stub(2100.0, mode="serial", checks=[{"id": "a.one", "status": "pass"}, {"id": "b.two", "status": "pass"}])
    gui_ran = [{"id": "a.one", "status": "pass", "duration_s": 10.0}, {"id": "g.ui", "status": "pass", "duration_s": 200.0},
               {"id": "k.bk", "status": "pass", "duration_s": 100.0}]
    cases = {
        "fast parallel, no reference -> pass": (_stub(1400.0), 1400.0, None, "pass"),
        "exactly the limit -> fail": (_stub(1500.0), 1500.0, None, "fail"),
        "fake slow wall -> fail": (_stub(1600.0), 1600.0, None, "fail"),
        "statuses equal the serial run -> pass": (_stub(1400.0), 1400.0, serial_ref, "pass"),
        "a status differs from the serial run -> fail": (_stub(1400.0), 1400.0, differ, "fail"),
        "serial reference run, slow -> pass (limit not applied)": (_stub(2100.0, mode="serial"), 2100.0, None, "pass"),
        "GUI/backups ran, adjusted under -> pass": (_stub(1700.0, checks=gui_ran, gui_skipped=()), 1700.0, None, "pass"),
        "GUI/backups ran, adjusted over -> fail": (_stub(1900.0, checks=gui_ran, gui_skipped=()), 1900.0, None, "fail"),
        "filtered run -> fail": (_stub(100.0, filt=("x",)), 100.0, None, "fail"),
    }
    out, bad = {}, []
    for name, (doc, wall, ref, want) in cases.items():
        got = judge(doc, wall, reg, ref, "stub")["status"]
        out[name] = {"want": want, "got": got}
        if got != want:
            bad.append(name)
    # reference lookup: same clean commit only, the other schedule only
    import tempfile
    with tempfile.TemporaryDirectory(prefix="hs-fullwall-") as td:
        sd, ld = Path(td, "serial"), Path(td, "last")
        sd.mkdir(), ld.mkdir()
        (sd / "results.json").write_text(json.dumps(_stub(2100.0, mode="serial")))
        (ld / "results.json").write_text(json.dumps(_stub(1400.0)))
        look = {
            "parallel finds the serial run of its commit": (load_reference(_stub(1.0)["meta"], sd, ld)[0] is not None, True),
            "serial finds the parallel run of its commit": (load_reference(_stub(1.0, mode="serial")["meta"], sd, ld)[0]
                                                            is not None, True),
            "another commit has no reference": (load_reference(_stub(1.0, sha="b" * 40)["meta"], sd, ld)[0] is None, True),
            "a dirty tree has no reference": (load_reference(_stub(1.0, sha="a" * 40 + "-dirty")["meta"], sd, ld)[0]
                                              is None, True),
        }
    for name, (got, want) in look.items():
        out[name] = {"want": want, "got": got}
        if got != want:
            bad.append(name)
    return {"cases": out, "wrong": bad}


def record(doc: dict, wall_s: float, reg: dict) -> dict:
    """report.full_run_wall for this run (post-run): judge this full-tier run, or, on a filtered run that selects it,
    the kept last full run. Always includes the self-test."""
    st = selftest()
    m = doc["meta"]
    if is_full_tier_run(m):
        ref, why = load_reference(m)
        rec = judge(doc, wall_s, reg, ref, why)
        rec["evidence"]["judged"] = "this run"
    else:
        from .runner import LAST_FULL_RUN
        try:
            kept = json.loads((LAST_FULL_RUN / "results.json").read_text())
        except (OSError, ValueError):
            kept = None
        if kept is None:
            rec = {"id": ID, "capability": "harness-reporting", "status": "pass", "duration_s": 0.0,
                   "evidence": {"judged": "nothing: not a full run and no kept full run; self-test only"}}
        else:
            ref, why = load_reference(kept["meta"])
            rec = judge(kept, float(kept["meta"].get("wall_s") or 0.0), reg, ref, why)
            rec["evidence"]["judged"] = "the kept last full run (harness/out/last-full-run)"
    rec["evidence"]["selftest"] = st
    if st["wrong"]:
        rec["status"] = "fail"
        rec["message"] = (rec.get("message", "") + "; " if rec.get("message") else "") + \
            f"self-test verdicts wrong: {st['wrong']}"
    return rec


def finalize(doc: dict, reg: dict) -> None:
    """After meta.wall_s is final: re-judge this run's record with it (the post-run record used the wall time at that
    point, a fraction of a second earlier). A record that flips to fail also fails report.full_run."""
    rec = next((r for r in doc["checks"] if r["id"] == ID), None)
    if rec is None or rec["evidence"].get("judged") != "this run":
        return
    ev = rec["evidence"]
    ev["final_wall_s"] = doc["meta"]["wall_s"]
    if not ev.get("limit_applied"):
        return
    final = float(doc["meta"]["wall_s"]) - sum((ev.get("gui_backups_checks_executed_s") or {}).values())
    ev["final_measured_s"] = round(final, 1)
    if final >= LIMIT_S and rec["status"] == "pass":
        msg = f"final wall {final:.1f} s ({ev['limit_mode']}) is not under {LIMIT_S:.0f} s"
        ev.setdefault("problems", []).append(msg)
        rec["status"], rec["message"] = "fail", msg
        fr = next((r for r in doc["checks"] if r["id"] == "report.full_run"), None)
        if fr is not None and fr["status"] == "pass":
            fr["status"], fr["message"] = "fail", f"not a clean full run: {ID}"
            fr["evidence"].setdefault("non_passing", []).append(ID)


def cli(path: str | None) -> int:
    """`hs full-run-wall [results.json]`: judge a results document (default the kept last full run); prints the record,
    exit 0 pass / 1 fail / 2 unreadable. Decision only: starts nothing."""
    from .registry import load_all
    from .runner import LAST_FULL_RUN
    p = Path(path) if path else LAST_FULL_RUN / "results.json"
    try:
        doc = json.loads(p.read_text())
        wall = float(doc["meta"]["wall_s"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"[hs] can't read a results document with meta.wall_s at {p}: {type(e).__name__}: {e}")
        return 2
    reg = load_all()
    ref, why = load_reference(doc["meta"])
    rec = judge(doc, wall, reg, ref, why)
    rec["evidence"]["judged"] = str(p)
    print(json.dumps({k: rec[k] for k in ("id", "status", "message") if k in rec}
                     | {"evidence": {k: v for k, v in rec["evidence"].items() if k != "schedule"}}, indent=2, default=str))
    return 0 if rec["status"] == "pass" else 1
