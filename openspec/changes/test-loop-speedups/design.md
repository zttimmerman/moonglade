# Design: test-loop speedups

- **Preflight:** `runner.gui_rule()` already decides whether GUI-marked checks run. `guienv.preflight_problem` takes that decision into account: locked or asleep only matters when the GUI checks are due.
- **Keep-awake:** `caffeinate -d -i -w <hs pid>`, started at the beginning of the run, ends with the run on its own, even after a SIGKILL. The check reads `pmset -g assertions`.
- **Last full run:** copied in `post_run` when `full_run` is true. The timing baseline reads it by default.
- **Orphans:** each harness run records its pid and its children's process group in a run file under `env/run/`. On the next start, a run file whose harness pid is dead marks the recorded processes as orphans. They are stopped with the existing stop path, `./play`'s SIGTERM handler, then `hs down`. A `./play` without such a record is the player's and is never touched.
- **Nightly:** `hs nightly` → 3× `hs run --tier full`, with each `results.json` copied to `harness/out/nightly/<date>/run<n>.json`, then `flakes.md`. A LaunchAgent plist goes in ~/Library/LaunchAgents only with `--install`.
- **Faster full run (B36):** first measure per-check wall time and which exclusive resources each check uses (stack up/down, client/Wine, screen, ports, DB fixtures). Then run the resource-free and stack-sharing groups concurrently where safe. If 25 minutes isn't reachable safely, report the measured best and the blockers. Don't weaken the requirement.

## As built
(filled in by the builders)
