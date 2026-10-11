---
name: hs-harness-checks
description: How to write Moonglade harness checks (harness/hslib/checks) that can actually fail. Use when adding or reviewing a @check, or mapping a spec scenario to a check.
---

# Writing harness checks

## Shape
```python
from ..registry import check, require
COVERS = ("modules/mod-moonglade/src/MoongladeX.cpp", "harness/hslib/x.py")   # this file's default ownership
@check("cap.thing", order=NNN, capability="<capability>", stack="up"|"down"|"any",
       layer="unit"|"integration"|"journey"|"gate")
def thing(ctx):
    """One line = the scenario's THEN, in plain words."""
    ev = {...}                      # commands, outputs, expected vs actual
    require(cond, "what was wrong", **ev)
    return ev                       # non-empty evidence is mandatory (report.schema)
```
- **Layer** (test-pyramid, harness-reporting "Test layers and run tiers"): the lowest layer that can prove the scenario.
  - `unit`: no server, client or Wine process (the unit tier samples for one and fails). Prefer a true unit test: Python under `harness/tests/unit/` (`unittest`, id `unit:py:<module.Class.method>`), C++ logic in `modules/mod-moonglade/src/logic/` with tests in `harness/tests/unit/cpp/` (`MG_TEST`, id `unit:cpp:<suite.name>`; the module must call the logic, never copy it), Lua add-on/glue logic under the stub in `harness/tests/unit/lua/` (id `unit:lua:<file.name>`). A scenario's `Check:` line may name a unit-test id. Each unit test ≤ 2 s. `parallel_safe=True` lets a unit check run in the 4-process pool.
  - `integration`: probe, SQL, CLI or a stand-in `./play`, on the shared server (`stack="up"`) or cold (`stack="down"`).
  - `journey`: only the fixed six (report.journeys); `budget_s` required.
  - `gate`: builds from empty, soaks, packaging, real-stack harness self-tests.
- **covers** (harness-reporting "Impact selection is fail-safe"): the product paths the check asserts on, the harness modules its file imports directly, config sections as `<conf>#<Section>` and fork.lock keys as `fork.lock#<key glob>`. A new product file must be owned (report.ownership); shared infrastructure falls to the smoke set.
- **Shared server**: `scope="character"` (it touches only its fixture characters; may run two at a time) or the default exclusive; `identity=True` gives a fresh account and two characters (`ctx["identity"]`; the saves helpers use it automatically). A test that changes shared state registers its undo in `ctx["undo"]`; the fixture's reset fails the test that leaks. Several scenarios may share one session: a session check lists `children=(ids)` and returns `{"children": {id: {status, message, evidence}}}` (session.run_children).
- **No fixed sleeps** over 1 s in acceptance code (report.no_fixed_sleeps): poll for the event with a bounding timeout; a real-time wait a spec requires goes on the allowlist with its spec citation.
- `order` sorts within a run segment (runner.segment: unit, cold, shared, profile groups, journeys, gate, guards). Check `harness/hs list`.
- Prefer driving the real CLI through `subprocess` (`harness/hs …`) so the user-facing path is what gets tested.

## Make it able to fail
- Assert the THEN clause itself, not a proxy. Past verifier FAILs:
  - `build.playerbots_active` looked for "no such command", but AzerothCore says "Command '%s' does not exist", so it could never fail.
  - `client.inspect` computed unrecorded binaries but never required that list to be empty.
- For every new check, run a counterexample (sabotage a throwaway copy, or an in-process patch) and confirm FAIL, then restore.
- Match real formats from source or logs: Wine `err:d3d`, mtld3d `ERROR mtld3d::…` (ANSI-coloured), AzerothCore SOAP faults.
- Read logs only after reaping the process; keep stdout and stderr in separate files (buffering makes merged tails non-deterministic).

## Flakiness is P0
- Thresholds and timeouts get recorded reasons. Run each new or changed test 3× before claiming green.
- Use generous, documented thresholds for stochastic behaviour (bots); keep raw samples in evidence.

## Coverage
- Every scenario: `Check: \`id\`` (a check, a session's child scenario id, or a unit-test id) or `[human-verify]` plus a reason. Only changes listed in `harness/active-changes` (plus archived specs, minus the requirements their deltas modify or remove) gate the run.

## Two lanes (second-stack, ADR-0011)
- `hs run` runs the checks in `harness/hslib/lanes.py` `LANE_B` in a second, isolated test stack (lane B: `env/lanes/b/`, ports 3317/3734/8095/7888) alongside lane A. Default: two lanes for unfiltered full runs, one otherwise; `--lanes 1|2` overrides. Lane B starts when lane A reaches the shared-server segment and joins before the first warm journey, `hs down`, first_play, gate, gui/`app.*` check or run guard. Records merge in run order; B's carry `"lane": "b"`, and `meta.lanes` holds the assignment, B's wall time and timeline, the preflight and any fallback.
- Write every check lane-aware: paths, ports and confs from `paths` (`P.RUN`, `P.LOGS`, `P.LANE_ETC`, `P.AUTH_PORT`, ...), processes via `stack.stack_processes()` / `launch.prefix_processes()` (this lane by default). Never hard-code `env/run`, `server/etc` or 3307/3724/8085/7878. Never write `env/player.json` or `env/settings.json` (lane B refuses).
- Adding a check to `LANE_B`: it must need only its own stack (no GUI, no real client, no build/baseline, no population/perf, no player-protection, order 34–379), and its entry says why it's safe there. `lane.table` enforces the rules; run the check with `--lanes 2` and `--lanes 1` and compare. A check that drives lane B itself (`lane.*`) stays below order 34.
- Memory: lane B starts only with A's stack up and pressure level 1, ≥ 5 GB available, swap headroom, and the Mac plugged in at ≥ 50 % (lanes.preflight_decision). Mid-run pressure ≥ 2 or swap growth > 1.5 GB stops B; its remaining checks run in A.
- Test switches `HS_TEST_LANE_MEMORY`, `HS_TEST_LANE_B`, `HS_TEST_LANE_TIMEOUT_S` work only in a nested `hs run`; everything else refuses them (`lane.refusal`).
- Timing: report.timing compares a two-lane run with the two-lane baseline (`checks_lanes2` in `harness/timing-baseline.json`) only; after the first clean two-lane full run, `harness/hs timing-baseline join --label <change>` creates it.
