# Spec Delta: play-client

## ADDED Requirements

### Requirement: The pristine client can't be written
The installed client folder (`client/`) SHALL be write-protected at the file-system level once installed. Only a deliberate
`hs client-install` (or bootstrap's client step) SHALL lift the protection, and it SHALL restore it before exiting, even when
the install fails. Building the play client from `client/` SHALL keep working, and the play client SHALL NOT inherit
the protection.

#### Scenario: Writes into the installed client are refused by the OS
- **GIVEN** an installed, protected client (a scratch install for the check)
- **WHEN** a plain process tries to overwrite, rename, delete or create a file in it, directly and through a symlinked path
- **THEN** each attempt fails, every file still matches the manifest, and a play client built from it is writable and patched normally
- Check: `client.pristine_locked`

#### Scenario: Reinstall lifts and restores the protection
- **GIVEN** a protected scratch install
- **WHEN** `hs client-install` reinstalls into it, and separately an install that fails partway (and one killed partway)
- **THEN** the folder is protected again afterwards in every case; the next install heals a killed one
- Check: `client.pristine_locked`

### Requirement: Client builders refuse to write into the pristine client
Every writer that produces play-client files (Wow.exe patches, MPQs, add-ons, config) SHALL refuse a destination that is the pristine client or lies inside it, decided by file-system identity (symlinks, `..` and case variants included), before writing anything.

#### Scenario: A build aimed at the pristine client through links is refused
- **GIVEN** a scratch "client" and a scratch build destination that resolves into it (a symlinked folder, a case variant, a `..` path)
- **WHEN** the play-client build runs against that destination
- **THEN** it stops before writing with a message naming the pristine client, and the scratch client is byte-identical
- Check: `client.build_refuses_pristine`

### Requirement: Every harness run proves the pristine client unchanged
The runner SHALL fingerprint `client/` (manifest comparison plus folder and file identity) at the start and end of every
run, and SHALL fail the run with a named check if anything changed.

#### Scenario: A changed pristine client fails the run
- **GIVEN** a run whose pristine-client root is a scratch install (test switch, refused outside the harness)
- **WHEN** a check changes one of its files during the run
- **THEN** the run fails with `report.client_pristine` naming the file; an untouched run passes it
- Check: `report.client_pristine`

### Requirement: Agents can't link or write the project's client and runtime folders
A Claude Code PreToolUse hook (committed in `.claude/settings.json`) SHALL block an agent's shell command or file edit that writes into `client/`, or that symlinks `client/`, `env/`, `data/` or `server/` of this repo into another folder. It SHALL allow normal harness and git commands, and it SHALL explain why when it blocks.

#### Scenario: The hook blocks the 2026-10-07 makeshift-copy pattern
- **GIVEN** the hook script and a table of sample commands (the actual 08:21 `ln -s $R/env env` loop, `cp x client/Wow.exe`, `mv client/...`, `sed -i` on a client file, an Edit tool call on a client path; and allowed ones such as `harness/hs run`, `shasum client/Wow.exe`, `git status`)
- **WHEN** each is fed to the hook as Claude Code would
- **THEN** each harmful one is blocked with a reason and each allowed one passes
- Check: `repo.agent_guard_hook`
