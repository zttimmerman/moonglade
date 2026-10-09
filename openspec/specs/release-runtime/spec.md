# release-runtime Specification

## Purpose
TBD - created by archiving change release-first-launch. Update Purpose after archive.

## Requirements

### Requirement: One resolver for dev and release paths
`harness/hslib/paths.py` SHALL be the only place that decides the mode and resolves every runtime path. A git checkout SHALL always be dev mode with today's layout unchanged. A release payload (the `release-build` `VERSION` file at the payload root, no `.git`) SHALL be release mode: APP is the payload folder inside `Moonglade.app`, and every path Moonglade writes SHALL be under HOME (`~/Library/Application Support/Moonglade`). The HOME location SHALL be overridable only by a test switch that normal entry points refuse.

#### Scenario: Dev checkout keeps today's layout
- **GIVEN** the repo checkout (with `.git`) and no test switches
- **WHEN** the resolver's full path table (every path and port `paths.py` exports, for lanes A and B) is compared with the table recorded from the parent commit
- **THEN** the mode is dev and every entry is identical
- Check: `release.mode_resolution`

#### Scenario: Payload resolves to release mode with everything writable under HOME
- **GIVEN** a copy of the packaged `Moonglade.app` and a scratch HOME given through the test switch inside a harness run
- **WHEN** the payload's Python prints the resolver's table
- **THEN** the mode is release; every writable path (run, logs, tmp, server confs, MySQL datadir, client-data, Wine runtime and prefix, client copy, play client, settings, `player.json`, backups) is under the scratch HOME; every read-only input (server binaries, `mysql/`, `sql/`, Python, harness, patches, client-mods) is under APP; and none names the dev repo or `/opt/homebrew`
- Check: `release.mode_resolution`

#### Scenario: Default HOME without a test switch
- **GIVEN** the payload and no test switch
- **WHEN** the resolver is asked for HOME without creating anything
- **THEN** HOME is `~/Library/Application Support/Moonglade` of the current user, and that folder is neither created nor modified by the check
- Check: `release.mode_resolution`

### Requirement: Test switches can't reach a normal start
Every test switch added by this change (scratch HOME, scripted folder pick, download URL override, injected disk/Mac facts, scratch ports) SHALL be refused by `./play`, `harness/hs`, the dev app and the release app unless the explicit test flag is given inside a live harness run, and SHALL never apply to the PLAYER account or the real HOME.

#### Scenario: Switches refused outside a harness run
- **GIVEN** each new switch set in the environment, without the test flag or outside a live harness run
- **WHEN** `./play`, `harness/hs`, the dev app (test hook only) and the release app's first launch start
- **THEN** each refuses or ignores the switch with a message naming it, starts nothing, and writes nothing outside its log
- Check: `release.test_switches_refused`

### Requirement: Release servers run from the payload
In release mode the harness SHALL start the payload's `mysqld` (datadir in HOME), `authserver` and `worldserver` with confs rendered into HOME; SHALL pass every module key as an `AC_*` environment variable; SHALL set `SourceDirectory` to the payload's `sql/`, `MySQLExecutable` to the payload's `mysql/bin/mysql` and `OPENSSL_CONF` to a payload or HOME file; and SHALL read nothing from `/opt/homebrew`, `/usr/local`, `vendor/`, `server/`, `build/` or `data/` of any checkout.

#### Scenario: Stack comes up from the payload alone
- **GIVEN** a set-up scratch HOME, scratch ports, and a `sandbox-exec` profile denying reads of `/opt/homebrew`, `/usr/local` and the dev repo's `vendor/`, `server/`, `build/`, `data/`, `env/` (except the scratch folders) and writes outside the scratch folders
- **WHEN** release mode brings the stack up with the m0 profile and then down
- **THEN** worldserver reaches SOAP readiness; the running `mysqld`'s executable is the payload's; the DB updater's log names the payload's `sql/`; a playerbots key set only through `AC_*` (m0's random-bot count) is in effect; and shutdown is graceful
- Check: `release.stack_from_payload`

#### Scenario: Without the AC_* keys the module setting is not in effect (counterexample)
- **GIVEN** the same setup with the test switch that drops the `AC_*` module variables
- **WHEN** the stack comes up
- **THEN** the check sees the playerbots `.conf.dist` default instead of m0's value and reports the difference
- Check: `release.stack_from_payload`

### Requirement: APP is read-only and movable
Release mode SHALL never write inside APP. Moving the app after setup SHALL keep working with the same HOME, without setting up again.

#### Scenario: Runs from a read-only volume
- **GIVEN** `Moonglade.app` on a read-only disk image (`hdiutil attach -readonly`) at a path with spaces, and a scratch HOME
- **WHEN** first launch runs headless (the test hook, scripted folder pick, downloads from a loopback server), then a play loop with the harness's stand-in client starts, reaches ready and quits
- **THEN** both succeed, no log line reports a read-only or permission error, and the disk image's contents hash the same before and after
- Check: `release.app_readonly`

#### Scenario: Moved after setup
- **GIVEN** a scratch HOME set up from the app at one path
- **WHEN** the app is moved to a second path and the stand-in play loop runs from there
- **THEN** it reaches ready without running first launch again, and every path it uses is under the new APP or the same HOME
- Check: `release.app_moved`

#### Scenario: Gatekeeper translocation
- **GIVEN** a downloaded (quarantined) release opened from Downloads without moving it, on a Mac that never had Moonglade
- **WHEN** the friend opens it (after macOS's own "open anyway" steps)
- **THEN** first launch runs from the translocated path, HOME is the friend's, and nothing is written inside the app
- [human-verify] Gatekeeper refuses an ad-hoc-signed quarantined app before Moonglade runs, so translocation can't be reached by a check until `release-signing` (3/3) decides signing; that change turns this into an automated check. Until then Zach tries it once on a second user account.

### Requirement: Release play loop keeps everything in HOME
In release mode the play loop SHALL use HOME's play client, Wine runtime and prefix, settings and `player.json`, and backups SHALL be written to HOME with the payload's `mysqldump`.

#### Scenario: Play, quit, backup in HOME
- **GIVEN** a set-up scratch HOME and scratch ports
- **WHEN** the release app runs the play loop with the stand-in client (test hook) and the stand-in exits
- **THEN** the stages reach ready, the stack stops gracefully, a backup appears in HOME's backups made with the payload's `mysqldump`, the PLAYER account is created in the scratch databases, and `settings.json` and `player.json` are in HOME
- Check: `release.play_standin`

#### Scenario: The real client starts from HOME
- **GIVEN** a scratch HOME set up from the dev Mac's `client/` (read-only source)
- **WHEN** the HOME play client's `Wow.exe` is started under HOME's Wine runtime and prefix with the harness's client launch smoke
- **THEN** its first window appears, and the client is closed with no stray Wine or `Wow.exe` process
- Check: `release.client_smoke`

### Requirement: Release work never touches the dev player's data
Release-mode checks and release mode itself SHALL NOT write `env/backups`, `env/player.json`, `env/settings.json`, `env/client-play`, `client/`, the live `acore_*` databases, or the real `~/Library/Application Support/Moonglade`.

#### Scenario: Player fingerprints unchanged
- **GIVEN** fingerprints at the start of the run of `env/backups`, `env/player.json`, `env/settings.json`, `env/client-play`, the `client/` manifest, `CHECKSUM TABLE` of the live `acore_characters` and `acore_auth` (or their datadir files when lane A's mysqld is down), and the real HOME's existence and listing
- **WHEN** every `release.*` and `firstlaunch.*` check of the run has finished
- **THEN** every fingerprint is unchanged
- Check: `release.player_data_untouched`
