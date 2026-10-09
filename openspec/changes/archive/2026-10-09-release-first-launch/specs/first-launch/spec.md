## ADDED Requirements

### Requirement: First launch sets up from the friend's own client
When the release app opens and HOME isn't fully set up, it SHALL show the first-launch window (the launcher's loading-window style) instead of starting the game. In order, each step skipped when already done: check this Mac and free disk; ask for the friend's 3.3.5a client folder (or zip); check it is Windows 3.3.5a build 12340 enUS with the existing client inspection; copy it into HOME and verify the copy against its manifest; download and verify the pinned Wine runtime and server data; set up the databases; build the play client; write default settings. It SHALL end with a screen saying setup is done and that the folder they gave can now be deleted, and a Play button. Later opens SHALL go straight to the play loop.

#### Scenario: Fresh HOME to ready
- **GIVEN** an empty scratch HOME, the app copy, the dev Mac's `client/` as the friend's folder (read-only source), a loopback HTTP server serving the cached pinned Wine runtime and server data, and the launcher's test hook with a scripted folder pick
- **WHEN** the release app opens
- **THEN** the hook records the first-launch window, every step in order with progress, and a final "done" screen whose text says the given folder can be deleted; HOME holds a client copy matching the source's manifest, the play client, server data matching its pin, the Wine runtime matching its pin, databases that pass the bootstrap database check, and default settings; and the app exits 0 with nothing from Moonglade left running
- Check: `firstlaunch.flow`

#### Scenario: The "you can delete it" line comes only after the copy is verified
- **GIVEN** the same setup with the test switch that corrupts one byte of the client copy before verification
- **WHEN** first launch runs
- **THEN** the client step fails with a plain message, the "can be deleted" text is never shown, and the next launch redoes the client copy
- Check: `firstlaunch.flow`

#### Scenario: Second open plays
- **GIVEN** a scratch HOME that first launch completed
- **WHEN** the release app opens again (test hook, stand-in client)
- **THEN** no first-launch window is shown and the play loop's stages run to ready
- Check: `firstlaunch.second_open_plays`

#### Scenario: Looks and reads right
- **GIVEN** a Mac user account with no Moonglade HOME
- **WHEN** Zach opens a packaged release and goes through first launch with his own client folder
- **THEN** the window matches the launcher's style, every line is plain language, progress is believable, and the macOS folder-access prompt (if any) makes sense
- [human-verify] Look, wording and the system privacy prompt (TCC asks once per folder, outside our control and not scriptable without changing TCC) are judged by a person. Checked at the next human gate.

### Requirement: The friend's folder is never written
First launch SHALL only read the folder or zip the friend gives. It SHALL NOT create, change, move or delete anything in it, including `WTF/Config.wtf` or caches (the existing pristine-client rules, applied to the friend's folder).

#### Scenario: Source fingerprint unchanged
- **GIVEN** a scratch client folder (an APFS clone of `client/` in a scratch dir) fingerprinted (every path, size, mtime, mode; hashes of `.exe`/`.dll`/`.wtf`) and a sandbox profile denying writes to it
- **WHEN** first launch completes from it, and again when it is killed during the client copy and resumed
- **THEN** the fingerprint is unchanged and the sandbox log shows no denied write to it
- Check: `firstlaunch.source_untouched`

### Requirement: Wrong or unusable clients are refused in plain language
First launch SHALL refuse, before copying anything, a folder or zip without `Wow.exe`, a client of another version or build, a non-enUS client, a path that doesn't exist or can't be read. The message SHALL say what was found, what is needed (WoW 3.3.5a, build 12340, English) and that no files were changed, and SHALL let the friend pick again.

#### Scenario: Each bad client is refused
- **GIVEN** scratch fakes: an empty folder, a text file, a folder whose `Wow.exe` is another version (the client's own `Repair.exe`), a deDE-shaped zip, a missing path, an unreadable folder
- **WHEN** each is given to first launch through the scripted folder pick
- **THEN** each shows a message naming the problem in plain words (the found version for the wrong build), offers to choose again, and HOME holds nothing but the log
- Check: `firstlaunch.client_refusals`

### Requirement: The Mac and the disk are checked up front
Before asking for the client, first launch SHALL check Apple Silicon, macOS 26.2 or newer (the Wine runtime's minimum, `bootstrap.MIN_MACOS`), Rosetta, nothing of Moonglade running, and that HOME's volume has room for the measured fixed part (server data, its download, databases, Wine, headroom; measured in task 4.1 and cited in design.md). Right after the client is chosen, and before anything is copied, it SHALL check that the volume has room for the whole setup: the given client's size plus that fixed part. It SHALL NOT install anything system-wide; when Rosetta is missing it SHALL say how to install it.

#### Scenario: Each preflight problem stops before writing
- **GIVEN** injected facts (test switch): an Intel Mac, macOS 26.1, no Rosetta, Moonglade already running, and free space 1 GB below the need
- **WHEN** first launch starts with each
- **THEN** each stops with a plain message (for disk: how many GB are needed and how many are free on which disk), and HOME holds nothing but the log
- Check: `firstlaunch.preflight`

#### Scenario: The disk need follows the client's size
- **GIVEN** two scratch client folders whose sizes differ by a known amount, and an injected free space
- **WHEN** preflight runs for each
- **THEN** the needed GB differ by that amount, and equal client size + the cited fixed part
- Check: `firstlaunch.preflight`

### Requirement: Downloads are pinned and show progress
The Wine runtime and server data SHALL be downloaded from their `fork.lock` pins only, verified by SHA-256 before use, with progress (bytes done of total) shown in the window. A wrong hash or a failed download SHALL stop with a plain message, keep nothing unverified, and continue from that step on the next launch.

#### Scenario: Progress reaches the total
- **GIVEN** the loopback HTTP server serving the cached pinned files
- **WHEN** first launch downloads them
- **THEN** the hook records progress events per download whose bytes never go backward and end at the file's size, and the window's bar never moves backward
- Check: `firstlaunch.downloads`

#### Scenario: Wrong bytes refused
- **GIVEN** the loopback server serving a file with one byte changed
- **WHEN** first launch downloads it
- **THEN** it stops with a plain message that the download was damaged, the file is removed, and the next launch downloads it again
- Check: `firstlaunch.downloads`

#### Scenario: No connection
- **GIVEN** the download URL override pointing at a closed loopback port
- **WHEN** first launch reaches the download
- **THEN** it stops with a message saying it couldn't download and to check the internet connection and open Moonglade again, and the next launch (server reachable) continues from that step without redoing earlier ones
- Check: `firstlaunch.downloads`

### Requirement: First launch is resumable and kill-safe
Killing first launch at any point SHALL leave nothing the next launch counts as done unless it is complete and verified, and the next launch SHALL continue and finish. A MySQL datadir is reset only when first launch created it in an unfinished attempt and it has no player characters (the AI players' own characters, on their `rndbot*` accounts, don't count; the count is taken on a clone of the datadir, never on the datadir itself); otherwise first launch stops and says so.

#### Scenario: Kill at each phase, then resume
- **GIVEN** a scratch HOME
- **WHEN** first launch is SIGKILLed (with its children) during the client copy, the server-data download, the server-data unpack and the database setup, each time followed by a normal launch
- **THEN** each following launch finishes; earlier finished steps are not redone (their outputs' inodes and mtimes are unchanged); no `.part`, `.hs-part` or temp folder is left; and the end state passes the same checks as the fresh run
- Check: `firstlaunch.kill_resume`

#### Scenario: A datadir with characters is never reset (counterexample)
- **GIVEN** a scratch HOME whose database step isn't marked done but whose `acore_characters` has a player's character (not on an AI player's `rndbot*` account)
- **WHEN** first launch reaches the database step
- **THEN** it stops with a plain message, and the datadir's files are unchanged
- Check: `firstlaunch.kill_resume`
