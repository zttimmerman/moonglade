"""world-population + performance-budget checks (m2-living-world tasks 2.1, 4.1).

All five are tier `slow` and share ONE population session (hslib/population.py): the first of them to run does
the cold start + warm-up + 10-minute sampling, the rest read the cached result from ctx. They run last (order
400+), after every m0-profile check, so the population profile never leaks into other checks; the runner's final
`hs down` stops it."""
import json
import math
import re

from .. import config, paths as P, population as pop
from ..registry import check, require

MIN_PER_ZONE = 3          # spec: each starting zone and capital has at least 3 online random bots
SIZE_FRACTION = 0.95      # spec: both counts >= 95% of N
MOVED_YD = 20.0           # spec: changed position by more than 20 yards ...
MOVED_FRACTION = 0.60     # ... for at least 60% of bots
PROGRESS_FRACTION = 0.25  # spec: at least 25% gained XP, a level, or completed a quest
TUNING_DOC = P.ROOT / "docs" / "research" / "m2-population-tuning.md"


def _zone_counts(snap: dict) -> dict[int, int]:
    areas = pop.dbc.area_table()
    counts: dict[int, int] = {}
    for b in snap["bots"].values():
        z = pop.dbc.top_zone(areas, b["zone"])
        counts[z] = counts.get(z, 0) + 1
    return counts


def zone_coverage(snap: dict, tg: dict) -> tuple[list[dict], list[str]]:
    """Per target zone: online random bots in it at the snapshot; problems for any below MIN_PER_ZONE, and for a
    target set that isn't 8 starting zones + 8 capitals."""
    counts = _zone_counts(snap)
    rows, probs = [], []
    for kind, want in (("starting_zones", 8), ("capitals", 8)):
        if len(tg[kind]) != want:
            probs.append(f"{kind}: derived {len(tg[kind])} zones from data, the spec names {want}")
        for z in tg[kind]:
            n = counts.get(z["id"], 0)
            rows.append({"kind": z["kind"], "zone_id": z["id"], "name": z["name"], "bots": n, "source": z["source"]})
            if n < MIN_PER_ZONE:
                probs.append(f"{z['kind']} {z['id']} {z['name']}: {n} bots (< {MIN_PER_ZONE})")
    return rows, probs


@check("pop.zones_covered", order=400, capability="world-population", stack="any", tier="slow", profile="population")
def zones_covered(ctx):
    """After the warm-up, each of the 8 race starting zones and 8 capitals (from DBC/playercreateinfo) has >= 3 online
    random bots; evidence lists per-zone counts and each zone ID's source."""
    s = pop.session(ctx)
    tg = s["targets"]
    rows, probs = zone_coverage(s["s0"], tg)
    saveall_at = s["s0"]["save"]["gm_at"] - s["t0"]
    areas = pop.dbc.area_table()
    all_zones = sorted(((z, n) for z, n in _zone_counts(s["s0"]).items()), key=lambda x: -x[1])
    ev = {"n": s["n"], "online_random_bots": len(s["s0"]["bots"]), "per_zone": rows, "problems": probs,
          "rules": tg["rules"], "dbc": tg["dbc"],
          "positions_captured_at_s_after_up": round(saveall_at, 1), "warmup_max_s": pop.WARMUP_MAX_S,
          "saveall": s["s0"]["save"], "zone_source": "characters.zone after .saveall (the server's zone for the "
                                                     "position) -> AreaTable.dbc top-level zone",
          "all_zones": [{"zone_id": z, "name": areas.get(z, {}).get("name"), "bots": n} for z, n in all_zones]}
    require(saveall_at <= pop.WARMUP_MAX_S, f"positions captured {saveall_at:.0f}s after hs up (> {pop.WARMUP_MAX_S}s)",
            **ev)
    require(not probs, f"{len(probs)} zone(s) under-populated or mis-derived", **ev)
    return ev


@check("pop.size_stable", order=401, capability="world-population", stack="any", tier="slow", profile="population")
def size_stable(ctx):
    """Online random bots at the end of the warm-up and 5 minutes later are both >= 95% of N, and no bot logged a
    fatal error (worldserver never restarted; no FATAL/ASSERTION in Playerbots.log, Server.log, worldserver.err)."""
    s = pop.session(ctx)
    n = s["n"]
    c0, c1 = len(s["s0"]["bots"]), len(s["s1"]["bots"])
    gap = s["s1"]["save"]["gm_at"] - s["s0"]["save"]["gm_at"]
    lg = s["logs"]
    ev = {"n": n, "count_end_of_warmup": c0, "count_5_min_later": c1, "threshold": math.ceil(SIZE_FRACTION * n),
          "gap_s": round(gap, 1), "worldserver": s["worldserver"], "logs": lg, "ramp": s["warmup"]["ramp"],
          "non_bot_before": s["non_bot_before"], "non_bot_after": s["non_bot_after"],
          "db_before": s["db_before"], "db_after": s["db_after"]}
    require(gap >= pop.ACTIVITY_GAP_S, f"second count only {gap:.0f}s after the first (< {pop.ACTIVITY_GAP_S}s)", **ev)
    require(c0 >= SIZE_FRACTION * n and c1 >= SIZE_FRACTION * n,
            f"online random bots {c0} then {c1}; need >= {SIZE_FRACTION:.0%} of N={n}", **ev)
    require(s["worldserver"]["same_process"], "worldserver process changed during the session (crash/restart)", **ev)
    fatal = lg["playerbots_fatal"] + lg["server_fatal"] + lg["worldserver_err_fatal"]
    require(not fatal, f"{len(fatal)} fatal log line(s)", **ev)
    require(s["non_bot_before"] == s["non_bot_after"], "non-bot accounts or characters changed during the session",
            **ev)
    return ev


def activity(s0: dict, s1: dict) -> dict:
    """Bots online in both snapshots: how many moved > MOVED_YD (any map change counts) and how many progressed
    (level up, more XP at the same level, or more rewarded quests)."""
    common = sorted(set(s0["bots"]) & set(s1["bots"]))
    moved, progressed, rows = 0, 0, []
    for g in common:
        a, b = s0["bots"][g], s1["bots"][g]
        dist = math.inf if a["map"] != b["map"] else math.dist((a["x"], a["y"], a["z"]), (b["x"], b["y"], b["z"]))
        prog = b["level"] > a["level"] or (b["level"] == a["level"] and b["xp"] > a["xp"]) \
            or b["rewarded"] > a["rewarded"]
        moved += dist > MOVED_YD
        progressed += prog
        rows.append({"guid": g, "name": a["name"], "map": [a["map"], b["map"]], "zone": [a["zone"], b["zone"]],
                     "moved_yd": None if math.isinf(dist) else round(dist, 1), "level": [a["level"], b["level"]],
                     "xp": [a["xp"], b["xp"]], "quests_rewarded": [a["rewarded"], b["rewarded"]]})
    k = len(common)
    return {"bots_in_both": k, "moved": moved, "progressed": progressed,
            "moved_fraction": round(moved / k, 3) if k else 0.0,
            "progressed_fraction": round(progressed / k, 3) if k else 0.0, "samples": rows}


@check("pop.bots_active", order=402, capability="world-population", stack="any", tier="slow", profile="population")
def bots_active(ctx):
    """Over 5 minutes after the warm-up, >= 60% of online random bots moved > 20 yd and >= 25% gained XP, a level,
    or a completed quest (raw per-bot samples in evidence)."""
    s = pop.session(ctx)
    act = activity(s["s0"], s["s1"])
    gap = s["s1"]["save"]["gm_at"] - s["s0"]["save"]["gm_at"]
    ev = {"gap_s": round(gap, 1), "thresholds": {"moved_yd": MOVED_YD, "moved_fraction": MOVED_FRACTION,
                                                 "progressed_fraction": PROGRESS_FRACTION}} | act
    require(gap >= pop.ACTIVITY_GAP_S, f"samples only {gap:.0f}s apart (< {pop.ACTIVITY_GAP_S}s)", **ev)
    require(act["bots_in_both"] > 0, "no bot online in both samples", **ev)
    require(act["moved_fraction"] >= MOVED_FRACTION,
            f"{act['moved_fraction']:.0%} of bots moved > {MOVED_YD:.0f} yd (< {MOVED_FRACTION:.0%})", **ev)
    require(act["progressed_fraction"] >= PROGRESS_FRACTION,
            f"{act['progressed_fraction']:.0%} of bots gained XP/level/quest (< {PROGRESS_FRACTION:.0%})", **ev)
    return ev


@check("perf.server_budget", order=410, capability="performance-budget", stack="any", tier="slow",
       profile="population")
def server_budget(ctx):
    """With the population profile after warm-up, every 10 s sample over 10 minutes meets: worldserver memory <= 4.0 GB,
    mysqld memory <= 1.0 GB, server-reported world update p95 <= 100 ms and median <= 50 ms, host free+inactive >= 3 GB."""
    s = pop.session(ctx)
    samples = s["samples"]
    span = samples[-1]["t"] - samples[0]["t"] if samples else 0
    bad = pop.violations(samples)
    ev = {"n": s["n"], "limits": pop.LIMITS, "samples_taken": len(samples), "window_s": round(span, 1),
          "summary": pop.summarize(samples), "violations": bad[:40], "violation_count": len(bad),
          "sampler_errors": s["sampler_errors"], "metric_source": {
              "process_memory": "max(ps -o rss, proc_pid_rusage phys_footprint) of the worldserver/mysqld pids (population.mem_gb)", "host": "vm_stat Pages free + Pages inactive",
              "update_time": "SOAP `.server info`: Median and Percentiles (95, ...) over the server's last 500 "
                             "world-loop diffs (cs_server.cpp:283-289, UpdateTime.h:26)"},
          "samples": samples}
    need = pop.SAMPLE_WINDOW_S // pop.SAMPLE_EVERY_S + 1
    require(len(samples) >= need and span >= pop.SAMPLE_WINDOW_S - pop.SAMPLE_EVERY_S,
            f"{len(samples)} samples over {span:.0f}s; need {need} over {pop.SAMPLE_WINDOW_S}s", **ev)
    require(not s["sampler_errors"], "sampler errors", **ev)
    require(not bad, f"{len(bad)} budget violation(s): {bad[0] if bad else ''}", **ev)
    return ev


# ----------------------------------------------------------------------------------------------- tuning record

TUNING_BLOCK_RE = re.compile(r"```json tuning-runs\n(.*?)\n```", re.S)


def tuning_runs(text: str) -> list[dict]:
    """The machine-readable block of the tuning record: a ```json tuning-runs fence holding an object
    {"chosen_n", "runs": [{"n", "summary": population.summarize(...) output, "source"}], optional "config_ceiling":
    {"n", "reason"}}."""
    m = TUNING_BLOCK_RE.search(text)
    if not m:
        raise ValueError("no ```json tuning-runs block")
    return json.loads(m.group(1))


def run_exceeds(summary: dict) -> list[str]:
    """Which limits a measured run's peak (worst sample) broke, recomputed from its numbers."""
    out = []
    for k, (op, lim) in pop.LIMITS.items():
        q = summary.get(k, {})
        worst = q.get("max") if op == "<=" else q.get("min")
        if worst is None:
            continue
        if (op == "<=" and worst > lim) or (op == ">=" and worst < lim):
            out.append(f"{k} {'max' if op == '<=' else 'min'} {worst} vs {op} {lim}")
    return out


def tuning_problems(doc: dict, n: int) -> list[str]:
    probs = []
    runs = doc.get("runs", [])
    if doc.get("chosen_n") != n:
        probs.append(f"record's chosen_n {doc.get('chosen_n')} != the population profile's N {n}")
    at_n = [r for r in runs if r.get("n") == n]
    if not at_n:
        probs.append(f"no measured run at N={n}")
    elif all(run_exceeds(r["summary"]) for r in at_n):
        probs.append(f"every recorded run at N={n} exceeds a limit")
    larger = [r for r in runs if r.get("n", 0) > n and run_exceeds(r["summary"])]
    ceiling = doc.get("config_ceiling")
    if not larger and not (ceiling and ceiling.get("n") == n and ceiling.get("reason")):
        probs.append(f"no measured run above N={n} that exceeded a limit, and no config-ceiling statement for N={n}")
    return probs


@check("perf.population_tuned", order=411, capability="performance-budget", stack="any", tier="slow",
       profile="population")
def population_tuned(ctx):
    """Given perf.server_budget passed at N this run, the tuning record shows a measured larger population that broke
    a limit (recomputed from its numbers), or states N is the playerbots config ceiling."""
    s = pop.session(ctx)
    n = s["n"]
    budget_bad = pop.violations(s["samples"])
    ev = {"n": n, "config_population_n": config.POPULATION_N, "record": str(TUNING_DOC.relative_to(P.ROOT)),
          "this_run_budget_violations": len(budget_bad)}
    require(n == config.POPULATION_N, f"session ran N={n}, the profile's N is {config.POPULATION_N} "
                                      f"(is {config.POPULATION_N_ENV} set?)", **ev)
    require(not budget_bad, "the budget measurement at N did not pass this run (GIVEN not met)", **ev)
    require(TUNING_DOC.exists(), "tuning record missing", **ev)
    try:
        doc = tuning_runs(TUNING_DOC.read_text())
    except ValueError as e:
        require(False, f"tuning record unreadable: {e}", **ev)
    ev["runs"] = [{"n": r.get("n"), "exceeds": run_exceeds(r.get("summary", {})), "source": r.get("source")}
                  for r in doc.get("runs", [])]
    ev["chosen_n"], ev["config_ceiling"] = doc.get("chosen_n"), doc.get("config_ceiling")
    probs = tuning_problems(doc, n)
    ev["problems"] = probs
    require(not probs, "; ".join(probs), **ev)
    return ev
