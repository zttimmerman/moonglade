## Why
A full harness run takes 36–48 min, and about 88% of it needs the one server stack and Wine prefix (B36). ADR-0011 (accepted for Phase 1, 2026-10-08) adds a second isolated stack, "lane B", so server and stand-in client checks run alongside lane A. Expected full run: ~25–27 min.

## What Changes
- `HS_LANE` (a|b) selects a lane. Lane A is today's layout, unchanged. Lane B has its own root (`env/lanes/b/`), mysqld datadir (an APFS clone of a lane-B template, never the player's data), ports (3317/3734/8095/7888), Wine prefix clone, run/log/pid/lock files and server confs (module keys via `AC_*` env).
- Server processes are matched by lane (`-c` config path), so one lane's `down` never touches the other.
- `hs lane init|status|down|reset` manages lane B.
- `hs run` splits checks: a reviewed `LANE_B` table runs in a lane-B worker between two barriers (after build/setup, before the GUI/app phase and player-protection checks). Results are merged as if serial. `--lanes 1` keeps the serial run.
- A memory and power preflight (ADR-0011 thresholds) decides whether lane B runs. Otherwise the run is serial, and says why.
- Phase 1 only: no real game client in lane B.

## Capabilities
### New Capabilities
- `harness-lanes`: isolated second stack and two-lane scheduling.

## Impact
harness/hslib (paths, stack, config, db, launch, procs, runreg, runner, play, nightly, new lanes.py/lane.py), cli, some checks' GIVENs (lane-aware), timing baseline (lane-mode label). No game data, no client changes.

## Spec-change log
