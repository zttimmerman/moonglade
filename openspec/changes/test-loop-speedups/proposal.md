# Test-loop speedups

## Why
On 2026-10-07 Zach said: "we waste so much time on failed tests then hour long build and verification loops", and "we gotta have a way around this unlock thing. It's a major blocker." That day, three verifier rounds were lost to environment state rather than to the code:
- a locked screen, which meant 45 minutes of full run wasted;
- a race reading the unified log;
- dependencies on Mac uptime.

A clean full run takes 35–46 minutes, and its results.json was overwritten twice before `timing-baseline join` could use it. Zach agreed to all four parts of this change ("Sure", "Start it").

## What changes
1. **The screen only matters when it's needed.** The GUI preflight refuses a locked screen or a sleeping display only when this run will actually execute GUI-marked checks; the existing GUI path rule decides that. Low battery while on battery power still always refuses. Other full runs start normally on a locked screen, since every non-GUI check has passed with the screen locked.
2. **The screen stays awake during runs.** Every `hs run` holds a display-sleep assertion for its whole duration, so a screen that is unlocked at the start stays unlocked. `hs keep-awake [--hours N]` holds the same assertion for an agent work session, so one unlock in the morning lasts.
3. **The last full run is kept.** Every unfiltered full run also writes `harness/out/last-full-run/` (results.json and summary.md), which later filtered runs never overwrite. `hs timing-baseline join` defaults to it.
4. **Orphans after a kill are healed.** If a harness run is SIGKILLed, the `./play`, Wine and server processes it started no longer block the next run with "something from this repo is already running". The next run identifies them as orphans of a dead harness run, stops them cleanly, and proceeds.
5. **A nightly flake hunt.** `hs nightly` runs the unfiltered full run 3 times on `main`. It writes `harness/out/nightly/<date>/` with a flake table listing every check whose status differed between the runs. It skips with a reason if the main checkout isn't on a clean `main`, if the stack is in use, or if the battery is low. `hs nightly --install` and `--uninstall` manage a user LaunchAgent; installing it is Zach's call, and agents never install it.
6. **A faster full run.** The target is under 25 minutes for an unfiltered full run with the GUI tier skipped; today it takes about 34.5 minutes. Checks that are safe to run alongside others do so, with no change to what each check proves. The timing guard's baseline is re-joined afterwards.

## Impact
- Code: `harness/hslib/runner.py`, `guienv.py`, `reporting.py`, `timingbase.py`, `__main__.py`, a new `nightly.py`, and check scheduling.
- No game or client changes.
- Capability: harness-reporting.

## Spec-change log
(none yet)
