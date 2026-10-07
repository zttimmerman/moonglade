# easy-setup Specification

## Purpose
TBD - created by archiving change easy-setup. Update Purpose after archive.

## Requirements

### Requirement: One-command bootstrap
`./bootstrap <client>` SHALL take a fresh clone of the repo, plus the player's own 3.3.5a (12340) client as a zip or a folder, to a ready `dist/Moonglade.app` with no other manual step, apart from approving Homebrew installs and macOS permission prompts. Each step SHALL be idempotent and resumable, and SHALL report progress and failures in plain language, naming the failed step, the reason, and what to do.

#### Scenario: Fresh clone to a playable app
- **GIVEN** a fresh `git clone` of HEAD in a scratch folder, an empty `env/`, `data/`, `build/`, `server/`, `client/` and `dist/` there, the installed Homebrew dependencies, and a local download cache holding the pinned files (each verified by sha256 before use)
- **WHEN** `./bootstrap <client>` runs there with `--yes`, using the dev Mac's installed client as the friend's client (read-only input)
- **THEN** it exits 0; the scratch clone has a built server, server data, an initialised database, the Wine runtime, the installed client (`client.inspect` passes there), the play client and a valid signed `dist/Moonglade.app`; and `./play` in the scratch clone with the stand-in client reaches "ready" and shuts down cleanly with nothing left running
- Check: `setup.fresh_clone`

#### Scenario: Re-running does nothing heavy
- **GIVEN** the scratch clone after a successful bootstrap
- **WHEN** `./bootstrap <client>` runs again
- **THEN** every step reports "already done", nothing is rebuilt, downloaded or copied (file timestamps of the build, data, client and app outputs are unchanged), and it finishes in under 2 minutes
- Check: `setup.fresh_clone`

#### Scenario: Interrupted, then resumed
- **GIVEN** a bootstrap in a scratch clone, killed (SIGKILL) during a long step (the server build or the client install)
- **WHEN** `./bootstrap <client>` runs again
- **THEN** it continues from the interrupted step, does not redo completed steps, and ends as in "Fresh clone to a playable app"
- Check: `setup.fresh_clone`

#### Scenario: Preflight refuses what can't work
- **GIVEN** each of these conditions, injected through a test switch that is refused outside the harness: an Intel Mac, no Rosetta, no Xcode Command Line Tools, no Homebrew, a missing Brewfile dependency without `--yes` and with the prompt declined, too little free disk space, a client that isn't 3.3.5a build 12340 enUS, and a path that isn't a zip or a folder
- **WHEN** `./bootstrap` runs
- **THEN** each one stops before changing anything, exits nonzero, and prints a plain message that names the problem and the fix (e.g. the exact install command); the repo tree is unchanged
- Check: `setup.preflight`

### Requirement: The same setup as Zach's
After bootstrap, the player's settings SHALL be Moonglade's documented defaults: vanilla mode on, the normal world size, and the patched front menu. Those defaults SHALL be defined in one place that both bootstrap and the docs use.

#### Scenario: Defaults after bootstrap
- **GIVEN** the scratch clone after a successful bootstrap
- **WHEN** its settings are read through the harness (`hs world-size`, era profile status, `hs client-mode`)
- **THEN** each equals the documented default, and the docs' defaults table is generated from, or checked against, the same source
- Check: `setup.fresh_clone`

### Requirement: Nothing tied to one machine
No tracked file SHALL contain a machine-specific absolute path, the dev Mac's user name or home folder, or another assumption about one machine. Tracked hooks and scripts SHALL work from any clone location.

#### Scenario: Tracked files are portable
- **GIVEN** every tracked file at HEAD
- **WHEN** they are scanned for `/Users/<name>`, `~<name>`, the dev Mac's user name, home folder and hostname, and hard-coded repo paths, with a short, reviewed allowlist (each entry with its reason)
- **THEN** there are no unallowlisted hits; and the scan reports a hit when one is planted in a scratch copy
- Check: `repo.portable`

#### Scenario: A friend sets it up
- **WHEN** someone other than the dev Mac's account (a friend on their own Mac, or a new macOS user account) follows the README quick start with their own client
- **THEN** they reach the Moonglade login screen and can play
- [human-verify] Needs a different machine or account and a person at the GUI; macOS permission prompts and Homebrew ownership differ per account.
