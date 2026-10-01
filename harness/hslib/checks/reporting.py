"""harness-reporting meta-checks. These run after all other checks, over the results document."""
import json
import re
import time

from .. import paths as P
from ..registry import REGISTRY

SCENARIO_RE = re.compile(r"^#### Scenario:\s*(.+)$", re.M)
CHECK_RE = re.compile(r"Check:\s*`([^`]+)`")
HUMAN_RE = re.compile(r"\[human-verify\]\s*(\S.{10,})")
POST_IDS = ("report.coverage", "report.schema", "report.full_run")


def spec_files():
    roots = [P.ROOT / "openspec" / "specs"] + [d / "specs" for d in (P.ROOT / "openspec" / "changes").iterdir()
                                                if d.is_dir() and d.name != "archive"]
    return sorted(f for r in roots if r.exists() for f in r.rglob("spec.md"))


def scenarios():
    """Yield (file, scenario name, check ids, human reason)."""
    for f in spec_files():
        text = f.read_text()
        heads = list(SCENARIO_RE.finditer(text))
        for i, m in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            nxt = re.search(r"^#{2,3} ", text[m.end():end], re.M)
            body = text[m.end(): m.end() + nxt.start()] if nxt else text[m.end():end]
            hm = HUMAN_RE.search(body)
            yield str(f.relative_to(P.ROOT)), m.group(1).strip(), CHECK_RE.findall(body), (hm.group(1).strip() if hm else None)


def coverage() -> tuple[bool, dict]:
    known = set(REGISTRY) | set(POST_IDS)
    bad, mapped, human, referenced = [], 0, [], set()
    for f, name, ids, reason in scenarios():
        referenced.update(ids)
        unknown = [i for i in ids if i not in known]
        if ids and not unknown:
            mapped += 1
        elif not ids and reason:
            human.append({"file": f, "scenario": name, "reason": reason})
        else:
            bad.append({"file": f, "scenario": name, "check_ids": ids, "unregistered": unknown,
                        "problem": "unregistered check id" if unknown else "no Check: id and no [human-verify] reason"})
    return not bad, {"scenarios_mapped": mapped, "human_verify": human, "incomplete": bad,
                     "referenced_ids": sorted(referenced), "spec_files": [str(f.relative_to(P.ROOT)) for f in spec_files()]}


def _validate(inst, schema, path="$"):
    errs = []
    t = schema.get("type")
    tmap = {"object": dict, "array": list, "string": str, "boolean": bool, "number": (int, float)}
    if t and not isinstance(inst, tmap[t]) or (t == "number" and isinstance(inst, bool)):
        return [f"{path}: expected {t}"]
    if "enum" in schema and inst not in schema["enum"]:
        errs.append(f"{path}: {inst!r} not in {schema['enum']}")
    if isinstance(inst, dict):
        errs += [f"{path}: missing {k}" for k in schema.get("required", []) if k not in inst]
        if len(inst) < schema.get("minProperties", 0):
            errs.append(f"{path}: fewer than {schema['minProperties']} properties")
        for k, sub in schema.get("properties", {}).items():
            if k in inst:
                errs += _validate(inst[k], sub, f"{path}.{k}")
    if isinstance(inst, list) and "items" in schema:
        for i, x in enumerate(inst):
            errs += _validate(x, schema["items"], f"{path}[{i}]")
    return errs


def post_run(doc, selected_ids):
    out = []
    t0 = time.time()
    ok, ev = coverage()
    out.append({"id": "report.coverage", "capability": "harness-reporting",
                "status": "pass" if ok else "fail", "evidence": ev, "duration_s": round(time.time() - t0, 2),
                **({} if ok else {"message": f"{len(ev['incomplete'])} scenario(s) without a valid check mapping"})})

    t0 = time.time()
    schema = json.loads((P.HARNESS / "results.schema.json").read_text())
    # validate the document as it will be written (including the coverage record)
    errs = _validate(json.loads(json.dumps({"meta": doc["meta"], "checks": doc["checks"] + out}, default=str)), schema)
    empty = [r["id"] for r in doc["checks"] + out if not r.get("evidence")]
    ok = not errs and not empty
    out.append({"id": "report.schema", "capability": "harness-reporting", "status": "pass" if ok else "fail",
                "evidence": {"schema": "harness/results.schema.json", "errors": errs[:50], "empty_evidence": empty,
                             "records_validated": len(doc["checks"]) + 1},
                "duration_s": round(time.time() - t0, 2), **({} if ok else {"message": "results.json fails schema"})})

    referenced = set(ev["referenced_ids"]) - set(POST_IDS)
    executed = {r["id"] for r in doc["checks"]}
    not_run = sorted(referenced - executed)
    failing = sorted(r["id"] for r in doc["checks"] + out if r["status"] != "pass")
    ok = doc["meta"]["full_run"] and not not_run and not failing
    out.append({"id": "report.full_run", "capability": "harness-reporting", "status": "pass" if ok else "fail",
                "evidence": {"full_run": doc["meta"]["full_run"], "spec_ids_not_executed": not_run,
                             "non_passing": failing, "checks_executed": len(executed),
                             "outputs": [str(P.RESULTS.relative_to(P.ROOT)), str(P.SUMMARY.relative_to(P.ROOT))]},
                "duration_s": 0.0,
                **({} if ok else {"message": "not a clean full run: " + ", ".join((["filtered"] if not doc["meta"]["full_run"] else []) + not_run + failing)[:300]})})
    return out
