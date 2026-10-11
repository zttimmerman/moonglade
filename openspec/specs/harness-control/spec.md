# harness-control Specification

## Purpose
Drive and inspect a running server for tests: GM commands, SQL assertions, and test fixtures (account, character, state changes).

## Requirements

### Requirement: GM command execution
`harness/hs gm "<command>"` SHALL send a GM command to worldserver via SOAP as a harness-owned GM account, print the server's output, and exit 0 on success and nonzero when the server reports the command failed or doesn't exist.

#### Scenario: Valid command returns output
- **GIVEN** the stack is up
- **WHEN** `harness/hs gm "server info"` runs
- **THEN** it exits 0 and the output contains the core revision string and player count
- Check: `gm.server_info`

#### Scenario: Invalid command is reported as failure
- **GIVEN** the stack is up
- **WHEN** `harness/hs gm "definitelynotacommand"` runs
- **THEN** it exits nonzero and the output includes the server's error text
- Check: `gm.invalid`

### Requirement: SQL assertions
The harness SHALL run SQL against the auth, world, and characters DBs and support assertions (row count, scalar equality, comparisons) that report expected vs. actual on failure.

#### Scenario: Query against world DB
- **GIVEN** the stack is up and databases initialized
- **WHEN** a check asserts `SELECT COUNT(*) FROM creature_template` > 1000
- **THEN** the assertion passes and the actual value is recorded as evidence
- Check: `sql.world_populated`

#### Scenario: Failing assertion reports expected vs actual
- **GIVEN** the stack is up
- **WHEN** the harness self-test asserts a deliberately false condition
- **THEN** the assertion result is FAIL with both expected and actual values in its evidence, and the self-test treats that as correct behaviour
- Check: `sql.assert_selftest`

### Requirement: Test fixtures
The harness SHALL create (idempotently) a test account and a test character, and drive character state through the server: set level, teleport, add item, and start a quest. Each state change SHALL be observable in the characters DB after the change is saved.

#### Scenario: Test account and character exist
- **GIVEN** the stack is up
- **WHEN** the fixture setup runs (twice in a row)
- **THEN** exactly one test account and one test character exist, created through the server (GM command / game protocol), not by direct row inserts
- Check: `fixture.account_character`

#### Scenario: Set level
- **WHEN** the harness sets the test character to level 10
- **THEN** `characters.level` for that character is 10
- Check: `fixture.level`

#### Scenario: Teleport
- **WHEN** the harness teleports the test character to a named location (e.g. Goldshire)
- **THEN** the character's saved map and coordinates are within 50 yards of that location's `game_tele` row
- Check: `fixture.teleport`

#### Scenario: Add item
- **WHEN** the harness gives the test character an item by entry ID
- **THEN** that item entry is owned by the character (inventory or mail) in the characters DB
- Check: `fixture.add_item`

#### Scenario: Start quest
- **WHEN** the harness starts a quest by ID for the test character
- **THEN** `character_queststatus` has that quest for the character with an incomplete status
- Check: `fixture.start_quest`

### Requirement: Test account logs out instantly
The harness test account SHALL hold the instant-logout permission and no other elevated permission. Accounts not created by the harness (e.g. the player's own) SHALL NOT be changed.

#### Scenario: Test character logout has no timer
- **GIVEN** the test character logged in through the probe, not in a rest area
- **WHEN** the probe requests logout
- **THEN** the server answers with an instant logout (no 20-second countdown), and the test account's security level is still 0 (not a GM)
- Check: `fixture.instant_logout`

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
