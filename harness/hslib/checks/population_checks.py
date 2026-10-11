"""world-population + performance-budget checks (m2-living-world tasks 2.1, 4.1).

All five are tier `slow` and share ONE population session (hslib/population.py): the first of them to run does
the cold start + warm-up + 10-minute sampling, the rest read the cached result from ctx. They run last (order
400+), after every m0-profile check, so the population profile never leaks into other checks; the runner's final
`hs down` stops it."""
import json
from pathlib import Path
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


# Declared ownership (harness-reporting "Impact selection is fail-safe"): what the checks of this file cover.
# First cut generated from the product paths they assert on and the harness modules this file imports directly
# (test-pyramid design 13.1); maintained by hand from here on. Shared infrastructure falls to the smoke set.
COVERS = (
    "harness/hslib/checks/population_checks.py",
    "harness/hslib/population.py",
    "harness/hslib/settings.py",
)


@check("pop.zones_covered", order=400, capability="world-population", stack="any", layer="gate", profile="population")
def zones_covered(ctx):
    """After the warm-up, each of the 8 race starting zones and 8 capitals (from DBC/playercreateinfo) has >= 3 online
    random bots; evidence lists per-zone counts and each zone ID's source."""
    s = pop.session(ctx)
    tg = s["targets"]
    rows, probs = zone_coverage(s["s0"], tg)
    saveall_at = s["s0"]["save"]["gm_at"] - s["t0"]
    areas = pop.dbc.area_table()
    all_zones = sorted(((z, n) for z, n in _zone_counts(s["s0"]).items()), key=lambda x: -x[1])
    ev = {"n": s["n"], "session_file": s.get("session_file"), "fresh_start": s["up"].get("fresh_start"),
          "online_random_bots": len(s["s0"]["bots"]), "per_zone": rows, "problems": probs,
          "rules": tg["rules"], "dbc": tg["dbc"],
          "positions_captured_at_s_after_up": round(saveall_at, 1), "warmup_max_s": pop.WARMUP_MAX_S,
          "saveall": s["s0"]["save"], "zone_source": "characters.zone after .saveall (the server's zone for the "
                                                     "position) -> AreaTable.dbc top-level zone",
          "all_zones": [{"zone_id": z, "name": areas.get(z, {}).get("name"), "bots": n} for z, n in all_zones]}
    require(saveall_at <= pop.WARMUP_MAX_S, f"positions captured {saveall_at:.0f}s after hs up (> {pop.WARMUP_MAX_S}s)",
            **ev)
    require(not probs, f"{len(probs)} zone(s) under-populated or mis-derived", **ev)
    return ev


@check("pop.size_stable", order=401, capability="world-population", stack="any", layer="gate", profile="population")
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


@check("pop.bots_active", order=402, capability="world-population", stack="any", layer="gate", profile="population")
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


@check("perf.server_budget", order=410, capability="performance-budget", stack="any", layer="gate",
       profile="population")
def server_budget(ctx):
    """With the population profile after warm-up, every 10 s sample over 10 minutes meets: worldserver <= 6.0 GB,
    mysqld <= 1.0 GB, worldserver+mysqld+authserver <= 7.0 GB (memory = max(RSS, footprint)), server-reported world
    update p95 <= 100 ms and median <= 50 ms; host free+inactive is recorded (not limited)."""
    s = pop.session(ctx)
    samples = s["samples"]
    span = samples[-1]["t"] - samples[0]["t"] if samples else 0
    bad = pop.violations(samples)
    ev = {"n": s["n"], "limits": pop.LIMITS, "recorded_not_limited": list(pop.RECORDED), "samples_taken": len(samples), "window_s": round(span, 1),
          "summary": pop.summarize(samples), "violations": bad[:40], "violation_count": len(bad),
          "sampler_errors": s["sampler_errors"], "metric_source": {
              "process_memory": "max(ps -o rss, proc_pid_rusage phys_footprint) per process (population.mem_gb); "
                                "stack_total = worldserver + mysqld + authserver",
              "host": "vm_stat Pages free + Pages inactive (recorded only)",
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
    {"chosen_n", "largest_measured": {"n", "fit"}, "runs": [{"n", "summary": population.summarize(...) output,
    "source"}]}."""
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


def worst_values(summary: dict) -> dict:
    """Per budget limit, the run's worst sample (max for <= limits, min for >= limits); None when not measured."""
    return {k: (summary.get(k, {}).get("max") if op == "<=" else summary.get(k, {}).get("min"))
            for k, (op, _lim) in pop.LIMITS.items()}


def tuning_problems(doc: dict, n: int) -> list[str]:
    """performance-budget "The default fits and the headroom is measured": runs at N and at least one larger size,
    each with a worst value for every limit, and a statement of the largest size measured and whether it fit
    (checked against that run's numbers)."""
    probs = []
    runs = doc.get("runs", [])
    if doc.get("chosen_n") != n:
        probs.append(f"record's chosen_n {doc.get('chosen_n')} != the population profile's N {n}")
    at_n = [r for r in runs if r.get("n") == n]
    larger = [r for r in runs if r.get("n", 0) > n]
    if not at_n:
        probs.append(f"no measured run at N={n}")
    if not larger:
        probs.append(f"no measured run at a size larger than N={n}")
    for r in at_n + larger:
        missing = [k for k, v in worst_values(r.get("summary", {})).items() if v is None]
        if missing:
            probs.append(f"run at N={r.get('n')} ({r.get('source')}) lacks worst values for {missing}")
    lm = doc.get("largest_measured")
    top = max((r.get("n", 0) for r in runs), default=0)
    if not isinstance(lm, dict) or "n" not in lm or "fit" not in lm:
        probs.append("no largest_measured {n, fit} statement")
    else:
        if lm["n"] != top:
            probs.append(f"largest_measured n {lm['n']} != largest run in the record {top}")
        fit = not any(run_exceeds(r.get("summary", {})) for r in runs if r.get("n") == top)
        if bool(lm["fit"]) != fit:
            probs.append(f"largest_measured says fit={lm['fit']}, the numbers say fit={fit}")
    return probs


@check("perf.population_tuned", order=411, capability="performance-budget", stack="any", layer="gate",
       profile="population")
def population_tuned(ctx):
    """Given perf.server_budget passed at the default N this run, the tuning record has measured runs at N and at a
    larger size, each with per-limit worst values, and states the largest size measured and whether it fit."""
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
    ev["runs"] = [{"n": r.get("n"), "worst": worst_values(r.get("summary", {})), "exceeds": run_exceeds(r.get("summary", {})),
                   "source": r.get("source")} for r in doc.get("runs", [])]
    ev["chosen_n"], ev["largest_measured"] = doc.get("chosen_n"), doc.get("largest_measured")
    probs = tuning_problems(doc, n)
    ev["problems"] = probs
    require(not probs, "; ".join(probs), **ev)
    return ev


# ----------------------------------------------------------------------------------------------- 2.2 checks

SIZE_TOLERANCE = 0.05       # spec: online random bots within 5% of the preset's size
SIZE_SETTLE_S = 420         # from the start step; login of 150 bots measured ~1-2 min; generous, recorded in evidence


def _size_case(case: str, settings_file: Path, want: int) -> dict:
    """./play's own world steps with $HS_SETTINGS = `settings_file` and no $HS_PROFILE: `play.choose_world()` (what
    Loop.run calls) then `launch.ensure_world()` (the stack step of launch.prepare, which Loop.run calls next). Only
    the client-side steps of ./play (Wine prefix, client config, Wow.exe) are left out. GIVEN state `case`:
    "running_m0" (a 10-bot m0 stack already up, as after `hs up`) or "cold" (nothing running)."""
    import time
    from .. import launch, play, stack
    ev: dict = {"case": case}
    if any(stack.stack_processes().values()):
        ev["down_before"] = {k: v for k, v in stack.down().items() if k == "remaining"}
    if case == "running_m0":
        ev["given_up"] = {k: v for k, v in stack.up(profile="m0").items() if k in ("profile", "worldserver")}
    ev["running_before"] = stack.running_profile()
    said: list[str] = []
    t0 = time.time()
    world = play.choose_world(say=said.append)
    ev["world"] = world
    started = launch.ensure_world(say=said.append, profile=world["profile"], population_n=world["population_n"])
    ev["ensure_world"] = {"restarted_world": started.get("restarted_world"),
                          "stack": started["stack"] if started["stack"] == "already up" else
                          {k: v for k, v in started["stack"]["up"].items()
                           if k in ("profile", "population_n", "worldserver", "ready_s")}}
    ev["said"] = said
    ev["running_profile"] = stack.running_profile()
    counts, inband = [], 0
    while time.time() - t0 < SIZE_SETTLE_S and inband < 3:
        time.sleep(10)
        c = pop.online_bots()
        counts.append({"t_s": round(time.time() - t0), "online_random_bots": c})
        inband = inband + 1 if abs(c - want) <= SIZE_TOLERANCE * want else 0
    ev |= {"counts": counts, "inband_polls": inband}
    ev["down_after"] = {k: v for k, v in stack.down().items() if k == "remaining"}
    return ev


@check("pop.size_setting", order=395, capability="world-population", stack="any", layer="gate", profile="population")
def size_setting(ctx):
    """With a (scratch) settings file saying `small`, ./play's world steps (play.choose_world -> launch.ensure_world)
    run the population profile with online random bots within 5% of the small preset, both from a cold start and
    when a 10-bot m0 stack was already running (the world server is restarted instead of keeping the wrong world);
    with no settings file the normal preset is used."""
    import os
    import tempfile
    from .. import settings, stack
    ev = {"presets": config.WORLD_SIZES, "user_settings_file_read": False,
          "entry": "play.choose_world() -> launch.ensure_world(), $HS_SETTINGS=<scratch>, no $HS_PROFILE"}
    saved = {k: os.environ.pop(k, None) for k in ("HS_PROFILE", settings.ENV, config.POPULATION_N_ENV)}
    tmp = Path(tempfile.mkdtemp(prefix="hs-settings-"))
    want = config.WORLD_SIZES["small"]
    try:
        # 1. no settings file -> normal (the function ./play calls to choose its world; play.py Loop.run)
        os.environ[settings.ENV] = str(tmp / "missing.json")
        from .. import play
        ev["no_file"] = play.choose_world(say=lambda _m: None)
        # 2. small -> ./play's world steps, from an already-running m0 stack and from cold
        f = tmp / "settings.json"
        f.write_text(json.dumps({"world_size": "small"}))
        os.environ[settings.ENV] = str(f)
        ev["cases"] = [_size_case(case, f, want) for case in ("running_m0", "cold")]
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        if any(stack.stack_processes().values()):
            stack.down()
        for x in tmp.iterdir():
            x.unlink()
        tmp.rmdir()
    ev |= {"want": want, "tolerance": SIZE_TOLERANCE}
    nf = ev["no_file"]
    require(nf["profile"] == "population" and nf["population_n"] == config.WORLD_SIZES["normal"],
            f"no settings file -> {nf['profile']} N={nf['population_n']}, want population N="
            f"{config.WORLD_SIZES['normal']}", **ev)
    for c in ev["cases"]:
        tag = c["case"]
        w = c.get("world") or {}
        require((w.get("source") or {}).get("file") == str(tmp / "settings.json"),
                f"{tag}: the world size was not read from the scratch settings file", **ev)
        require(w.get("profile") == "population" and w.get("population_n") == want,
                f"{tag}: ./play chose {w.get('profile')} N={w.get('population_n')}, want population N={want}", **ev)
        rp = c["running_profile"] or {}
        require(rp.get("profile") == "population" and rp.get("n") == want
                and rp.get("digest") == config.profile_digest("population", want),
                f"{tag}: running profile {rp.get('profile')} N={rp.get('n')}, want population N={want}", **ev)
        last = c["counts"][-1]["online_random_bots"] if c["counts"] else 0
        require(abs(last - want) <= SIZE_TOLERANCE * want and c["inband_polls"] >= 3,
                f"{tag}: online random bots {last} (want {want} +/- {SIZE_TOLERANCE:.0%}, stable for 3 polls)", **ev)
        rw = c["ensure_world"]["restarted_world"]
        if tag == "running_m0":
            before = c["running_before"] or {}
            require(before.get("profile") == "m0", f"GIVEN not met: running world before ./play was {before}", **ev)
            require((rw or {}).get("was", {}).get("profile") == "m0"
                    and any("restarting the world server" in m for m in c["said"]),
                    "./play kept or did not report replacing the running m0 world", **ev)
        else:
            require(c["running_before"] is None, "GIVEN not met: a worldserver ran before the cold ./play", **ev)
            require(not rw, "cold ./play reported a restart", **ev)
    return ev


@check("pop.fresh_start_scope", order=403, capability="world-population", stack="any", layer="gate",
       profile="population")
def fresh_start_scope(ctx):
    """Every random-bot character online in the session was either reset by the population start's fresh-world step
    (all reset characters at the reset level, no randomize/teleport timer rows left) or created after it (a new
    character: level 1, no timers); non-bot account/character checksums are identical across the reset and the
    session."""
    s = pop.session(ctx)
    fs = s["up"].get("fresh_start") or {}
    ev = {k: v for k, v in fs.items() if k != "reset_guids"}
    ev |= {"session_non_bot_before": s["non_bot_before"], "session_non_bot_after": s["non_bot_after"]}
    require(fs, "no fresh-start record in the population start", **ev)
    reset = set(fs.get("reset_guids") or [])
    top = max(reset) if reset else 0
    online = sorted(set(s["s0"]["bots"]) | set(s["s1"]["bots"]))
    was_reset = [g for g in online if int(g) in reset]
    created_after = [g for g in online if int(g) not in reset and int(g) > top]
    neither = [g for g in online if int(g) not in reset and int(g) <= top]
    ev |= {"online_bots": len(online), "online_reset": len(was_reset), "online_created_after_reset": len(created_after),
           "online_neither": neither[:20]}
    levels = {int(k): int(v) for k, v in (fs.get("bot_levels_after") or {}).items()}
    lvl = fs.get("reset_level")
    require(levels and set(levels) == {lvl} and sum(levels.values()) == len(reset),
            f"bot levels after reset {levels}, want all {len(reset)} reset characters at {lvl}", **ev)
    require(fs.get("timer_rows_left") == 0, f"{fs.get('timer_rows_left')} randomize/teleport timer rows left", **ev)
    require(not neither, f"{len(neither)} online bot(s) existed before the reset but were not reset", **ev)
    require(fs.get("non_bot_before") == fs.get("non_bot_after"), "non-bot data changed during the reset", **ev)
    require(s["non_bot_before"] == s["non_bot_after"], "non-bot data changed during the session", **ev)
    return ev
