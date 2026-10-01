# Spec Delta: server-build

## Purpose
Build the pinned AzerothCore (Playerbot fork) server and its modules from source on macOS ARM64, reproducibly, from one command.

## ADDED Requirements

### Requirement: Clean build from pinned sources
The harness SHALL build authserver and worldserver from an empty build directory using the core and module SHAs pinned in `fork.lock`, applying every patch in `patches/core/` and linking every module in `modules/`.

#### Scenario: Clean build succeeds
- **GIVEN** an empty build directory
- **WHEN** `harness/hs build --clean` runs
- **THEN** it exits 0 and executable `authserver` and `worldserver` binaries exist in the install dir
- **AND** the core checkout in `vendor/` is at exactly `core_sha` and mod-playerbots at exactly `playerbots_sha`
- Check: `build.clean`

#### Scenario: Unpinned or dirty vendor tree is rejected
- **GIVEN** the vendor core checkout is at a SHA other than `core_sha`, or has uncommitted changes that aren't from `patches/core/`
- **WHEN** `harness/hs build` runs
- **THEN** it resets the checkout to the pinned state (or fails with a message naming the mismatch) and never builds from an unpinned tree
- Check: `build.pin_enforced`

### Requirement: mod-playerbots is compiled in and active
The built worldserver SHALL include mod-playerbots, and the module SHALL initialize at startup.

#### Scenario: Playerbots module loads
- **GIVEN** a successful build and a running worldserver
- **WHEN** the harness inspects the worldserver startup log and the databases
- **THEN** the log shows mod-playerbots initialization with no fatal errors, and the playerbots DB tables exist
- **AND** a playerbots GM command sent via the GM interface is recognized (not "no such command")
- Check: `build.playerbots_active`
