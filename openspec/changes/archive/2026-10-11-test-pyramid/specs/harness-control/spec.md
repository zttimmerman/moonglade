## ADDED Requirements

### Requirement: Integration tests share one server start
The integration layer SHALL run against one shared stack started once per config profile in a run: the stack-down tests that exercise `./play`'s own start and stop first (each its own session), then one `hs up` for every shared-server test, grouped so that tests needing another config profile run together after the default-profile tests with one restart per profile. Each shared-server test SHALL get its own freshly created, uniquely named account and characters from the fixture (never PLAYER, never the harness's fixed HSTEST character unless the scenario names it), and those SHALL be removed at the end of the run. Tests that declare themselves character-scoped (they change only their own characters and read nothing global) MAY run two at a time; every other test runs alone.

#### Scenario: One start per profile, fresh characters per test
- **GIVEN** a pr run whose selection includes at least five shared-server tests on the default profile and one on another profile
- **WHEN** the integration layer runs
- **THEN** the run's stack timeline shows exactly one stack start for the default-profile tests and one worldserver restart for the other profile; each test's evidence names characters created for it and used by no other test; after the run none of those characters remain; PLAYER's characters are byte-identical before and after
- Check: `fixture.shared_server`

### Requirement: Reset between shared-server tests is checked, cheap and fail-closed
Between shared-server tests the fixture SHALL verify the world's shared state: the world is running and pausing is enabled (`.moonglade pause status`), the running config profile is the test's profile, the save interval and other runtime settings the module exposes are at their defaults, and no probe session of an earlier test is still in the world except ones the earlier test registered as lingering. A test that changes shared state SHALL register its undo. When the check finds leaked state, the test that ran last SHALL fail with the leaked state as evidence, and the fixture SHALL restart the worldserver before the next test. The reset SHALL take at most 1 s median and 3 s at most per test, recorded.

#### Scenario: A leaking test is caught and the next test is clean
- **GIVEN** a scratch integration test that pauses the world and changes the save interval without registering an undo, followed by `gm.server_info`
- **WHEN** both run in a pr run
- **THEN** the scratch test fails naming "world left paused" and "save interval changed", the worldserver is restarted before `gm.server_info`, which passes; the reset times of the run are recorded with their median and maximum within budget
- Check: `fixture.reset_leak`

### Requirement: Warm journeys reuse a running stack only in the harness
`./play` SHALL leave an already-running stack up at exit, instead of stopping it, only when a harness run asks for it with the test switch `HS_PLAY_KEEP_STACK` together with the harness test flag and the test account. A normal `./play`, the app, `hs` outside a harness run, and any run for the player's account SHALL refuse the switch with a nonzero exit before anything starts. `journey.first_play` SHALL never use it.

#### Scenario: The keep-stack switch is refused outside the harness
- **GIVEN** `HS_PLAY_KEEP_STACK=1`
- **WHEN** a normal `./play`, `./play --account player` inside a harness run, and the app's play loop start
- **THEN** each exits nonzero naming the switch before any server or Wine process starts (sampled at 10 Hz), and the player's files are untouched
- **AND** inside a harness run with the test account and a running stack, `./play` with a stand-in exits 0 and the stack's pids are unchanged afterwards
- Check: `fixture.keep_stack_refused`

### Requirement: Server-side waits are controllable in tests
Integration tests SHALL shorten server-side timers that a scenario doesn't fix in real time by changing the world for that test only (a scratch spawn with a short respawn time, a short save interval, a per-test character instead of waiting for an offline grace period) and SHALL restore them through the reset rule. Real-time durations a scenario states (a 20 s pause, the 20 s logout countdown, a 30 s stay) SHALL NOT be shortened; several scenarios MAY share one such wait when one setup establishes all their GIVENs.

#### Scenario: The respawn scenario uses a short scratch respawn and leaves nothing behind
- **GIVEN** the pause respawn test
- **WHEN** it runs
- **THEN** its evidence shows a scratch spawn whose respawn time is at most 15 s, the two pauses each longer than that time, the creature dead at each resume and back after the unpaused remainder; after the test the scratch spawn is gone from `creature` and the world's respawn data has no row for it
- Check: `pause.respawn_frozen`
