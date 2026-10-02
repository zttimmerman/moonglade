# Spec Delta: play-launcher

## Purpose
A single command that takes the player from nothing running to in-game and back to nothing running, fully offline.

## ADDED Requirements

### Requirement: One-command play loop
`./play` SHALL bring the stack up (waiting for readiness), configure the client for localhost, ensure the player's account exists, launch the client under Wine, and block until the client exits. Then it SHALL shut the stack down gracefully and exit 0.

#### Scenario: Client exit shuts everything down
- **GIVEN** nothing from this repo is running
- **WHEN** `./play` runs and the client process exits normally
- **THEN** `./play` exits 0 within 120 s of the client exiting, worldserver's log shows a graceful halt, and no stack, Wine, or Wow.exe process from this repo remains, with ports 3307/3724/8085/7878 free
- Check: `play.loop_clean_exit`

#### Scenario: Interrupt shuts everything down
- **GIVEN** `./play` is running with the client up
- **WHEN** `./play` receives SIGINT (Ctrl-C) or SIGTERM
- **THEN** the client is closed, the stack shuts down gracefully, `./play` exits within 120 s, and nothing from this repo remains running
- Check: `play.interrupt_clean_exit`

#### Scenario: Client crash still cleans up
- **GIVEN** `./play` is running
- **WHEN** the client process is killed (SIGKILL)
- **THEN** `./play` detects the exit, shuts the stack down gracefully, exits nonzero with a message naming the client crash, and nothing from this repo remains running
- Check: `play.client_crash_cleanup`

#### Scenario: Startup failure doesn't launch the client
- **GIVEN** the stack can't reach readiness (e.g. worldserver can't find its data)
- **WHEN** `./play` runs
- **THEN** it exits nonzero with the server's log excerpt, never starts Wow.exe, and leaves nothing running
- Check: `play.startup_failure`

### Requirement: Single instance
Only one `./play` loop SHALL run per repo at a time.

#### Scenario: Second invocation refuses
- **GIVEN** `./play` is running
- **WHEN** a second `./play` is started
- **THEN** the second exits nonzero within 5 s with a message naming the running instance's pid, and the first is unaffected
- Check: `play.single_instance`

### Requirement: Offline operation
The play loop SHALL need no network access beyond 127.0.0.1.

#### Scenario: Loop works with the network denied
- **GIVEN** outbound connections to any non-loopback address are denied for `./play` and all its child processes
- **WHEN** the play loop runs through startup, client launch, and shutdown
- **THEN** every step succeeds, and no process from the loop attempted a non-loopback connection (per the denial log)
- Check: `play.offline`

#### Scenario: Zach plays offline
- **GIVEN** the Mac's Wi-Fi is turned off
- **WHEN** Zach runs `./play`, logs in, plays briefly, and quits the game
- **THEN** the game works, and the terminal shows a clean shutdown with nothing left running
- [human-verify] Needs a person at the GUI client with networking physically disabled; this is the M1 acceptance demo. It isn't a separate gate in KICKOFF §8, so it goes to Zach at the next gate.

### Requirement: Personal player account
`./play` SHALL log the player in with a personal account stored in a gitignored local file, created on first use (not a GM, no characters), and SHALL never use the harness test account. The harness SHALL never modify or delete the personal account.

#### Scenario: First run creates the personal account; later runs reuse it
- **GIVEN** no player file exists (a scratch player-file location is used, so the real `env/player.json` is untouched)
- **WHEN** `./play` runs twice with the stand-in client and exits normally each time
- **THEN** the first run creates the player file (mode 0600) and exactly one matching account with security level 0 and no characters, and prints the login. The second run creates nothing new, and its account id is unchanged. The password never appears in `env/run/play-last.json`. A name of `HSTEST` or `HS_GM` in the player file is refused with a nonzero exit.
- Check: `play.player_account`

#### Scenario: Harness runs never touch the personal account
- **GIVEN** a personal account exists
- **WHEN** the fixture reset (`fixture.account_character`) runs
- **THEN** the personal account's row (id, username, salt/verifier, security) and its characters are byte-identical before and after
- Check: `play.player_account_untouched`
