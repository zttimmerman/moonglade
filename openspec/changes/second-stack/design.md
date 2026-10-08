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

## As built: lane layer (task group 1)
- `paths.py`: `HS_LANE` read once; `layout(lane)` gives every per-lane path/port; module constants are the current lane's. Lane-B paths: `env/lanes/b/{run,logs,tmp,etc,mysql,template,my.cnf,client*.cnf,wineprefix-<layer>}`. Shared: `GLOBAL_RUN/harness-runs` (run registry), player paths, `server/`, client data. `lane_refusal()` allows lane B only with `HS_HARNESS_RUN=1` and an `HS_HARNESS_RUN_ID` naming a live run file (pid + start time); `hs` (all commands but `lane status|down`), `./play` and `stack.up()` refuse; an unknown `HS_LANE` value exits at import.
- `config.write_all` (lane B): auth/world confs in `env/lanes/b/etc`, module confs mirrored there for readers, and `etc/ac-env.json` = AC_* for the union of every profile's module keys (profile value, else `.conf.dist` default) + `PlayerbotsDatabaseInfo` + `CommandServerPort=0`; `stack._spawn` adds them (and strips any inherited AC_*). `RealmServerPort`/`WorldServerPort` rendered from P in both lanes. Renders are atomic (temp + rename).
- `stack.stack_processes(lane)`: mysqld by `--defaults-file=<lane my.cnf>`, auth/world by binary + `-c` path (no `-c` = lane A). Lane-B `mysql_up` clones the template when the datadir is missing; only `hs lane init` may `--initialize`.
- `lanestack.py` (`hs lane init|status|down|reset`): from lane A each action re-runs itself with `HS_LANE=b`. `init` clones lane A's Wine prefix (only with A's Wine stopped), builds the template from scratch (~80 s), clones it. `reset` = down + fresh clone (~0.1 s).
- Orphan healing (`runreg.heal`): stops orphan lane workers (argv token `hslib.lane`) first, then per lane: this lane as before, other lanes via `runreg.lane_down(lane)` only when every process of that lane is an orphan.
- Lane-aware GIVENs: `play_checks.play_processes(lane)` by the process's `HS_LANE`; `launch.prefix_processes(prefix=)`, Wow rule needs a prefix match; `nightly.busy` covers both lanes. Lane B never writes `env/player.json` (needs `HS_PLAYER_FILE`) or `env/settings.json`.
- Checks `lane.*` sit at order 32-33, before the scheduler's start barrier (34), and drive lane B themselves.
