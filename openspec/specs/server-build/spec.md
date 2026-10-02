# server-build Specification

## Purpose
Build the pinned AzerothCore (Playerbot fork) server and its modules from source on macOS ARM64, reproducibly, from one command.

## Requirements

### Requirement: Clean build from pinned sources
The harness SHALL build authserver and worldserver using the core and module SHAs pinned in `fork.lock`, applying every patch in `patches/core/` and linking every module in `modules/`. A full run SHALL perform a from-empty build whenever the build-input hash (the server-build pins in fork.lock, i.e. core and playerbots repo/branch/SHA; `patches/core/`; `modules/` contents; cmake arguments and dependency links; compiler identity) differs from the hash recorded by the last passing from-empty build, or when `--force-clean` is given. Otherwise it SHALL prove the installed binaries were built from the current input hash and that an incremental build is a no-op.

#### Scenario: Clean build succeeds
- **GIVEN** an empty build directory (from-empty mode)
- **WHEN** `harness/hs build --clean` runs
- **THEN** it exits 0 and executable `authserver` and `worldserver` binaries exist in the install dir
- **AND** the core checkout in `vendor/` is at exactly `core_sha` and mod-playerbots at exactly `playerbots_sha`
- Check: `build.clean`

#### Scenario: Unchanged inputs skip the from-empty build only with proof
- **GIVEN** a previous passing from-empty build recorded input hash H, and the current inputs hash to H
- **WHEN** a full run executes `build.clean` without `--force-clean`
- **THEN** it records mode "verified-current", the installed binaries' stamp equals H, an incremental build makes no changes to the installed binaries, and the evidence states that no from-empty build was done
- **AND** when any input changes (e.g. a byte appended to a file under `modules/` in a sandbox copy), the computed hash differs from H and the check switches to from-empty mode
- Check: `build.clean_smart`

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

### Requirement: Login/logout race doesn't crash the world server
The built worldserver SHALL survive a character logging out before its login-time server-mail lookups complete.

#### Scenario: Immediate logout after login, repeated
- **GIVEN** a running server built with the core patch queue, and the test character with instant logout
- **WHEN** the probe logs the character in and requests logout immediately (no settle wait), 20 times in a row
- **THEN** worldserver is still running with the same pid, SOAP answers, no new crash report for worldserver exists, and every logout completed
- Check: `core.login_logout_race`

#### Scenario: The race reproduces without the patch
- **GIVEN** a scratch build of the pinned core **without** `0001-server-mail-guard-async-player.patch` (built in a separate build dir; the real install is untouched)
- **WHEN** the same immediate login/logout loop runs against it
- **THEN** that worldserver crashes within the 20 attempts (proving the check exercises the real defect), and the evidence includes the crash report's top frames naming `ServerMailMgr::SendServerMail` or the `server_mail.cpp` callback
- Check: `core.login_logout_race_baseline`

#### Scenario: Patch is applied by the pinned build
- **WHEN** `harness/hs build` syncs the vendor tree
- **THEN** `0001-server-mail-guard-async-player.patch` applies cleanly to `core_sha`, the patched source contains the GUID guard, and the build-input hash changes when the patch file changes
- Check: `core.patch_applied`
