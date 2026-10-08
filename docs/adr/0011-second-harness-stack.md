# ADR-0011: A second isolated harness stack ("lane B")

- Status: **accepted for Phase 1** (2026-10-08). Zach agreed to the direction on 2026-10-08 ("Yep. Your call" on doing it and its place in the queue). The Phase 0 spike (`docs/research/second-stack-spike.md`) measured one lane at 4.6 GB without a client and 5.2 GB with one, and found statically that 3.3.5a honours `realmlist host:port` (Grunt connect at 0x8cbb40 parses `:port`, default 3724). Phase 2 (real clients in lane B) turns on only after Phase 1 nightly runs show the memory preflight holding.
- Source: read-only design pass at main 0ab6a8b; timings from `harness/out/last-full-run` and test-loop-speedups B36 (`openspec/changes/archive/2026-10-07-test-loop-speedups/design.md`).

## Context
A full run takes 35–37 min. About 88% of it needs exclusive resources: ./play + client checks 1420 s, stack-up 394 s. Running file-only checks on the side saved nothing (c81013e, reverted). Everything that holds the run up is tied to one stack: one mysqld, one set of auth/world/SOAP ports, one Wine prefix, one `env/run`.

Two facts shape the design:
- worldserver loads module confs (playerbots, era) from the compiled-in `_CONF_DIR/modules` (`vendor/azerothcore-wotlk/src/common/Configuration/Config.cpp:711,766`), so `-c` alone can't separate them.
- AzerothCore lets any config key, including module keys, be overridden with `AC_<UPPER_SNAKE>` environment variables (`Config.cpp:435-443`).

## Decision
1. **`HS_LANE`** (default `a`) is read once in `paths.py`. Lane A is exactly today's layout.
2. **Lane B has its own:**
   - root `env/lanes/b/` (run, logs, tmp, etc);
   - mysqld datadir: an APFS clone of a lane-B template built from scratch with `--initialize-insecure` + AutoSetup. The player's datadir is never read or cloned.
   - ports 3317 / 3734 / 8095 / 7888;
   - Wine prefix (an APFS clone of A's, taken with Wine stopped);
   - pid/lock/profile files.
3. **Shared, read-only:** `server/bin`, client data, `client/`, the Wine runtime, `env/client-play`. Lane B never writes `env/player.json`, `env/settings.json`, `env/client-play`, `env/backups` or the live `acore_*` DBs. It refuses to run outside a harness run.
4. **Server config:** lane B renders its own `-c` confs and passes every module and DB-info key as `AC_*` env in `stack._spawn`. `config.render` becomes atomic. Process matching uses the `-c` path, not the binary name, so A's `down` can't kill B.
5. **Scheduler:** a reviewed `LANE_B` table in `hslib/lanes.py`, run by one worker process (`HS_LANE=b`) reusing `runner.execute`.
   - The worker starts after the build/setup checks and joins before the GUI/app phase and the player-protection checks.
   - Records are merged in (order, id) order.
   - `hs run --lanes 1` is the serial reference.
   - Lane B runs only if a memory preflight and the power guard allow it; otherwise the run falls back to one lane.
6. **Phases:**
   - **Phase 1:** server and stand-in checks in B (`play.*` stand-ins, `report.tiers`, `comp.*`, `era.*`, `lifecycle.persist`).
   - **Phase 2:** real-client login sessions in B, only after the spike proves the 3.3.5a client honours `realmlist host:port`.
   - All `app.*`, GUI, stock-mode, baseline, population and player-protection checks stay in A, with B idle.

## Alternatives
- **Shared mysqld with `_b` schemas.** Saves ~0.7 GB, but A's ./play checks stop mysqld, and test writes would sit beside the player's live DBs (the B25 lesson). Rejected.
- **Second install prefix with its own `_CONF_DIR`.** A second ~12 min build. Kept as the fallback if `AC_*` overrides misbehave.
- **Loopback alias 127.0.0.2.** A `sudo` system change, which would need Zach's OK. Phase 2 fallback only.
- **Docker/VM.** Against ADR-0002, and RAM-heavy.
- **Readiness polling (~100 s) and shared ./play sessions.** Complements, not substitutes.

## Consequences
- **Expected full run:** ~25–27 min after Phase 1, ~20–21 after Phase 2. Barriers, the GUI phase and contention rule out a true halving.
- **Meaning change:** "nothing from this repo is running" becomes "nothing from this lane". The timing baseline gets a lane-mode label and a re-join.
- **Disk:** ~0 at clone time, growing with copy-on-write.

## Memory preflight (from the spike)
Lane B starts only when, with A's stack up, the pressure level (`kern.memorystatus_vm_pressure_level`) is 1, free + inactive + speculative + purgeable is ≥ 5 GB (≥ 6 GB for a client in B), and swap has ≥ 1 GB free or has grown ≤ 1 GB since the run started. Mid-run, pressure level ≥ 2 or swap growth > 1.5 GB stops B from starting new checks; the remaining B checks run in A.

## Risks
- **Memory.** Lane B is ~4 GB without a client and ~5.5 GB with one, for a peak of ~10–11 GB on a 16 GB Mac that already swaps. The spike measures the m0 world + one Wow.exe. Population checks stay in A.
- **Phase 2.** Blocked if 3.3.5a ignores `:port` in realmlist.
- **Contention flakes** (e.g. `comp.assists`, F23): watch the nightly runs.
- **Orphan healing** must be per lane.
