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
