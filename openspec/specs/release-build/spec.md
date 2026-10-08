# release-build Specification

## Purpose
TBD - created by archiving change release-build. Update Purpose after archive.

## Requirements

### Requirement: Relocatable payload
Every Mach-O file in the release payload SHALL load only system libraries (`/usr/lib`, `/System`) or `@rpath`/`@executable_path`/`@loader_path` libraries shipped in the payload. No load command, rpath or embedded path SHALL name `/opt/homebrew`, `/usr/local` or `/Users/`, except entries on a reviewed allowlist with reasons.

#### Scenario: No absolute non-system paths
- **GIVEN** a payload built by `tools/release/package.sh`
- **WHEN** the check walks every Mach-O's `otool -L`/`LC_RPATH` and scans strings
- **THEN** there are zero forbidden paths outside the allowlist, and every `@rpath` dependency resolves inside the payload
- Check: `release.no_abs_paths`

#### Scenario: Runs without Homebrew
- **GIVEN** the payload copied to a scratch path containing spaces, and a `sandbox-exec` profile denying file reads under `/opt/homebrew` and `/usr/local`
- **WHEN** the bundled mysqld initialises a scratch datadir, the bundled mysql client queries it, `worldserver --version` and `authserver --version` run, and the bundled Python runs `-I -m hslib --help`
- **THEN** each succeeds, and nothing from the player's install or the dev stack is touched (ports and datadirs are scratch)
- Check: `release.runs_without_homebrew`

#### Scenario: Works from a second location
- **GIVEN** the same payload moved to another scratch path
- **WHEN** the same smoke runs
- **THEN** it succeeds again
- Check: `release.runs_without_homebrew`

### Requirement: No game files and source alongside
A release SHALL contain no client or game files (MPQ, `Wow.exe`, DBC, maps/vmaps/mmaps, client-data). Every release SHALL ship, as separate assets:
- the Moonglade source at the tag;
- the AzerothCore + mod-playerbots source at the `fork.lock` SHAs with our patches;
- the exact MySQL source tarball;
- `SHA256SUMS` covering every asset.

#### Scenario: Archive has no game files
- **GIVEN** a packaged release directory
- **WHEN** the archive listing is checked
- **THEN** no path matches the game-file patterns, and the download stays under 200 MB
- Check: `release.no_game_files`

#### Scenario: Source assets complete
- **GIVEN** a packaged release directory
- **WHEN** the check runs
- **THEN** every listed source asset exists and is in `SHA256SUMS` with a matching hash, the core source tarball's tree matches the pinned SHA plus the patches, and `THIRD_PARTY/` holds the licence texts of AzerothCore, mod-playerbots, MySQL, OpenSSL, Boost, Python and every other bundled library
- Check: `release.source_alongside`

### Requirement: Pinned inputs
The Oracle MySQL tarball, its source tarball and python-build-standalone SHALL be pinned by URL and sha256 in `fork.lock`. A download whose hash doesn't match SHALL fail the packaging run.

#### Scenario: Hash mismatch refuses
- **GIVEN** a pinned input whose downloaded bytes have a different sha256 (test switch: corrupt the cached copy)
- **WHEN** packaging fetches it
- **THEN** packaging fails, naming the input, and produces no payload
- Check: `release.pins`

### Requirement: Dev install untouched
Packaging SHALL NOT write the dev `build/`, `server/`, `data/`, `env/` (except its own `env/release/` scratch), `client/` or the player's play client, and SHALL NOT install or upgrade Homebrew packages locally.

#### Scenario: Dev tree fingerprints unchanged
- **GIVEN** fingerprints of `server/bin`, `build/CMakeCache.txt`, `env/player.json`, `env/client-play` and the `client/` manifest
- **WHEN** a full local packaging run completes
- **THEN** every fingerprint is unchanged
- Check: `release.dev_untouched`
