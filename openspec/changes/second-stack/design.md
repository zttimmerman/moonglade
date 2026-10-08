## Context
See ADR-0011 (accepted for Phase 1) and docs/research/second-stack-spike.md (collision table in the ADR draft history; memory numbers). Phase 1: no real client in lane B.

## Decisions
- `paths.py` reads `HS_LANE` once. Lane B overrides RUN/LOGS/TMP/ETC/datadir/ports/Wine prefix. Player paths and the run registry dir stay global.
- Template datadir `env/lanes/b/template` built by `hs lane init` (`mysqld --initialize-insecure` + AutoSetup on lane-B ports). Each run uses an APFS clone (`cp -c`) as `env/lanes/b/mysql`. The player's datadir is never read.
- worldserver/authserver confs are rendered into lane B's etc. Module and DB keys for lane B go in as `AC_*` env vars in `stack._spawn` (union of all profiles' keys). `config.render` writes atomically.
- Process matching: auth/world by `-c <lane etc>`; mysqld already by its my.cnf. The Wow rule in launch.py requires a prefix match.
- Realmlist row port comes from P (fixes the hard-coded 8085).
- Scheduler: `hslib/lanes.py` (LANE_B table, split, memory preflight) plus `python -m hslib.lane spec.json` worker (HS_LANE=b, reuses runner.execute). Barriers: start after order < 34; join before order ≥ 380, any gui/app check, and order ≥ 998. Worker timeout kills its process group, and B is torn down.
- Initial LANE_B (Phase 1, per ADR): stand-in `play.*` (loop_clean_exit, interrupt_clean_exit, client_crash_cleanup, startup_failure, single_instance, offline, player_account), `report.tiers`, `comp.*`, `era.*`, `lifecycle.persist`. The builder may drop any that turn out to depend on lane-A-only state, recording why.
- Timing: `report.timing` compares per-check times under a lane-mode label. Re-join the baseline after the first clean two-lane full run.
