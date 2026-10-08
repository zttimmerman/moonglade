## Context
F34: a 67 W charger loses to sustained full-core load on the M2 Pro (6 performance + 4 efficiency cores). Builders have been told to cap at -j6 and pause below 35 %; this makes that structural.

## Decisions
- **One module, `harness/hslib/power.py`:** `default_jobs(probe)`, `battery()` (reuses `guienv.battery`), and `guard(step_name, log)`, which blocks while below 35 %, polls every 60 s, resumes at 50 %, and raises `PowerStop` at ≤15 %. Hysteresis (35 → 50) stops it flapping.
- **Plugged in counts.** The incident drained while plugged in, so the pause ignores the power source. The existing start-up refusal (battery power, below 35 %) is unchanged.
- **Where the guard runs:** before each `cmake` step in `build._run_steps` and `baseline.build_install`, before each bootstrap step, and before each check in full/slow/gui runs (not quick: it is short and runs on builders' laps).
- **Desktop Macs:** an unknown percent never pauses.
- **Test switches:** `HS_TEST_POWER_CORES` (JSON), `HS_TEST_BATTERY_SEQ` (comma list like `40,34p,50`; `p` = plugged in), `HS_TEST_POWER_PLAN_ONLY`; poll interval 0 under the stub. Refused outside nested runs, like `HS_TEST_GUI_*`.

## Risks
- A paused run lengthens its wall time; the timing guard must ignore paused seconds (record the pause duration and subtract it).

## As built (B37)
- Plan-only mode (`HS_TEST_POWER_PLAN_ONLY=<scratch dir>`): `hs build` writes its plan and guard log to `<dir>/build.log`; `./bootstrap` (with `--allow-test-inject`) keeps step state in `<dir>/bootstrap-state.json`, and only its build step runs (as a plan-only `hs build` child).
- The runner strips the power test switches from its environment before running checks, so they can't reach the checks' own subprocesses.
- Paused time is recorded in `meta.power` and `meta.timing.paused_s` / `active_wall_s`, outside each check's timed duration. A stop writes partial results, brings the stack down (unless `--leave-up`) and exits 3.
