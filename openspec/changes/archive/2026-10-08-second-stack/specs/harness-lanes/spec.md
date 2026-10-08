## ADDED Requirements

### Requirement: Lane B is isolated from the player and from lane A
With `HS_LANE=b`, every runtime path (run, logs, tmp, server confs, pid/lock files, mysqld datadir, client.cnf, Wine prefix) SHALL resolve under `env/lanes/b/`, and every port SHALL be lane B's (3317/3734/8095/7888). Lane B SHALL never write `env/player.json`, `env/settings.json`, `env/client-play`, `env/backups`, `client/` or lane A's datadir, and SHALL refuse to start outside a harness run.

#### Scenario: Lane B paths and ports
- **GIVEN** `HS_LANE=b` inside a harness run
- **WHEN** the path layer is loaded
- **THEN** every runtime path is under `env/lanes/b/`, the ports are 3317/3734/8095/7888, and the player paths are unchanged
- Check: `lane.paths`

#### Scenario: Lane B leaves the player's files and lane A alone
- **GIVEN** fingerprints (mtime, size, hash) of the player files, lane A's datadir and `client/`
- **WHEN** lane B's stack starts, runs a fixture write and stops
- **THEN** every fingerprint is unchanged, and lane B's DB writes appear only in lane B's datadir
- Check: `lane.isolation`

#### Scenario: Lane B refused outside a harness run
- **GIVEN** `HS_LANE=b` without a harness run
- **WHEN** `hs up`, `./play` or `hs lane init` runs
- **THEN** each refuses with a message naming `HS_LANE`, before starting anything
- Check: `lane.refusal`

### Requirement: Lanes don't stop each other
Server processes and the Wine/Wow process rules SHALL be matched per lane, so `down`, orphan healing and "already running" GIVENs act only on their own lane.

#### Scenario: A's down leaves B up, and the reverse
- **GIVEN** both lanes' stacks are up
- **WHEN** lane A runs `down`
- **THEN** lane B's mysqld/auth/world stay up and answer on B's ports; then B's `down` leaves a restarted A up
- Check: `lane.independent_down`

#### Scenario: Orphan healing is per lane
- **GIVEN** a killed lane-B worker that left B's stack running
- **WHEN** the next harness run starts
- **THEN** it heals lane B's orphans and leaves a running lane A alone
- Check: `lane.orphans`

### Requirement: Two-lane scheduling
`hs run` SHALL run the checks listed in the reviewed `LANE_B` table in a lane-B worker. The worker starts after the build/setup checks and joins before the GUI/app checks and the player-protection checks. No GUI, `app.*`, stock-mode, baseline-build, population or player-protection check SHALL ever be in `LANE_B`. Results SHALL be merged in (order, id) order, with `meta.lanes` recording the assignment, lane-B wall time and the preflight. `--lanes 1` SHALL run everything serially in lane A.

#### Scenario: LANE_B table invariants
- **GIVEN** the `LANE_B` table and the check registry
- **WHEN** the table is checked
- **THEN** every entry exists, and none is gui, `app.*`, `client.stock_mode`, `client.launch_smoke`, a baseline/build check, a population/perf check or order ≥ 998
- Check: `lane.table`

#### Scenario: Two-lane run matches serial statuses
- **GIVEN** a nested run of a small mixed selection (two lane-B checks, two lane-A checks and one check after the join)
- **WHEN** it runs with `--lanes 2` and then `--lanes 1`
- **THEN** both give the same statuses per check, the two-lane `meta.lanes` shows the B checks ran in B, and B's checks ended before the post-join check started
- Check: `lane.merge`

#### Scenario: Unfiltered full run uses two lanes
- **GIVEN** an unfiltered full run whose preflight allows lane B
- **WHEN** it finishes
- **THEN** `meta.lanes` shows lane B ran every `LANE_B` check, and its total wall time is recorded next to the last serial full run's
- Check: `report.lanes`

### Requirement: Preflight and fallback
Lane B SHALL start only when the power guard allows it and memory meets ADR-0011's thresholds (pressure level 1, ≥ 5 GB available, swap headroom). Otherwise the run SHALL be serial and `meta.lanes` SHALL say why. If memory pressure rises mid-run, B SHALL start no new checks, and its remaining checks SHALL run in A.

#### Scenario: Low memory falls back to one lane
- **GIVEN** a stubbed memory probe below the threshold
- **WHEN** a nested two-lane run starts
- **THEN** it runs serially, every check still runs, and `meta.lanes.fallback` names the reason
- Check: `lane.preflight`

#### Scenario: Pressure mid-run moves remaining checks to A
- **GIVEN** a stubbed memory probe that turns to pressure level 2 after lane B's first check
- **WHEN** a nested two-lane run runs
- **THEN** B starts no further checks, the remaining B checks run in A, and all pass
- Check: `lane.preflight`

#### Scenario: Test switches can't leak
- **GIVEN** the lane test switches (stubbed memory probe, forced lane) set in the environment
- **WHEN** `hs`, `./play` or `./bootstrap` start outside a harness run
- **THEN** each refuses, naming the switch
- Check: `lane.refusal`
