---
name: hs-harness-checks
description: How to write Moonglade harness checks (harness/hslib/checks) that can actually fail. Use when adding or reviewing a @check, or mapping a spec scenario to a check.
---

# Writing harness checks

## Shape
```python
from ..registry import check, require
@check("cap.thing", order=NNN, capability="<capability>", stack="up"|"down"|"any")
def thing(ctx):
    """One line = the scenario's THEN, in plain words."""
    ev = {...}                      # commands, outputs, expected vs actual
    require(cond, "what was wrong", **ev)
    return ev                       # non-empty evidence is mandatory (report.schema)
```
- `order` sets the run sequence: destructive/"down" checks first, then lifecycle, then GM/SQL, fixtures, client, `lifecycle.down` (190), then client/RE checks (200+). Check `harness/hs list`.
- Prefer driving the real CLI through `subprocess` (`harness/hs …`) so the user-facing path is what gets tested.

## Make it able to fail
- Assert the THEN clause itself, not a proxy. Past verifier FAILs:
  - `build.playerbots_active` looked for "no such command", but AzerothCore says "Command '%s' does not exist", so it could never fail.
  - `client.inspect` computed unrecorded binaries but never required that list to be empty.
- For every new check, run a counterexample (sabotage a throwaway copy, or an in-process patch) and confirm FAIL, then restore.
- Match real formats from source or logs: Wine `err:d3d`, mtld3d `ERROR mtld3d::…` (ANSI-coloured), AzerothCore SOAP faults.
- Read logs only after reaping the process; keep stdout and stderr in separate files (buffering makes merged tails non-deterministic).

## Flakiness is P0
- Thresholds and timeouts get recorded reasons. Run the full harness twice before claiming green.
- Use generous, documented thresholds for stochastic behaviour (bots); keep raw samples in evidence.

## Coverage
- Every scenario: `Check: \`id\`` or `[human-verify]` plus a reason. Only changes listed in `harness/active-changes` (plus archived specs) gate the run.

## Two lanes (second-stack, ADR-0011)
- `hs run` runs the checks in `harness/hslib/lanes.py` `LANE_B` in a second, isolated test stack (lane B: `env/lanes/b/`, ports 3317/3734/8095/7888) alongside lane A. Default: two lanes for unfiltered full/slow runs, one otherwise; `--lanes 1|2` overrides. Lane B starts after the last check with order < 34 and joins before order ≥ 380, any gui/`app.*` check and order ≥ 998. Records merge in (order, id) order; B's carry `"lane": "b"`, and `meta.lanes` holds the assignment, B's wall time and timeline, the preflight and any fallback.
- Write every check lane-aware: paths, ports and confs from `paths` (`P.RUN`, `P.LOGS`, `P.LANE_ETC`, `P.AUTH_PORT`, ...), processes via `stack.stack_processes()` / `launch.prefix_processes()` (this lane by default). Never hard-code `env/run`, `server/etc` or 3307/3724/8085/7878. Never write `env/player.json` or `env/settings.json` (lane B refuses).
- Adding a check to `LANE_B`: it must need only its own stack (no GUI, no real client, no build/baseline, no population/perf, no player-protection, order 34–379), and its entry says why it's safe there. `lane.table` enforces the rules; run the check with `--lanes 2` and `--lanes 1` and compare. A check that drives lane B itself (`lane.*`) stays below order 34.
- Memory: lane B starts only with A's stack up and pressure level 1, ≥ 5 GB available, swap headroom, and the Mac plugged in at ≥ 50 % (lanes.preflight_decision). Mid-run pressure ≥ 2 or swap growth > 1.5 GB stops B; its remaining checks run in A.
- Test switches `HS_TEST_LANE_MEMORY`, `HS_TEST_LANE_B`, `HS_TEST_LANE_TIMEOUT_S` work only in a nested `hs run`; everything else refuses them (`lane.refusal`).
- Timing: report.timing compares a two-lane run with the two-lane baseline (`checks_lanes2` in `harness/timing-baseline.json`) only; after the first clean two-lane full run, `harness/hs timing-baseline join --label <change>` creates it.
