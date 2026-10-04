# Spec Delta: mac-app

## ADDED Requirements

### Requirement: One Hearthstone in the Dock
When the game runs from Hearthstone.app, the Dock SHALL show exactly one icon for it, Hearthstone's, from launch to quit, and activating Hearthstone by any route SHALL bring the game forward (ADR-0010).

#### Scenario: The game has no Dock icon of its own
- **GIVEN** the app opened with the GUI stand-in client (Wine's notepad under the same runtime and shim)
- **WHEN** the stand-in's window is up
- **THEN** `lsappinfo` reports the stand-in pid as a UIElement (no Dock icon) named Hearthstone, the launcher pid as the only Foreground process of this session with bundle id `local.hearthstone.app`, and no other process of this prefix as Foreground
- Check: `app.single_dock_icon`

#### Scenario: Hand-off when the game window appears
- **GIVEN** the app opened
- **WHEN** the stand-in's first window appears
- **THEN** the loading window is hidden and the stand-in becomes frontmost within 2 s, without user input
- Check: `app.handoff_focus`

#### Scenario: Clicking Hearthstone brings the game forward
- **GIVEN** the stand-in window is up and another app is frontmost
- **WHEN** the app is reopened through LaunchServices (as a Finder or Dock click does)
- **THEN** within 2 s the stand-in is frontmost, no "already running" dialog is shown, and the launcher keeps its single Dock icon
- Check: `app.reopen_focuses_game`

#### Scenario: A minimised game comes back on click
- **GIVEN** the stand-in window minimised (via a test-only shim notification)
- **WHEN** the app is reopened
- **THEN** the window is on screen and frontmost again
- Check: `app.reopen_unminimizes`

#### Scenario: Terminal ./play keeps a normal Dock icon with Hearthstone's identity
- **GIVEN** `./play` from a shell with the GUI stand-in (no app)
- **WHEN** the window is up
- **THEN** the stand-in is a normal Foreground app named Hearthstone, and the shim log records the icon replacement
- Check: `play.dock_identity`

#### Scenario: The shim stays in the runtime layer
- **GIVEN** any play run
- **THEN** `client.inspect` passes; the shim file lives outside `client/` and outside `env/wowsilicon-r17/` (whose pinned hash is unchanged); `DYLD_INSERT_LIBRARIES` is set only in the Wine environment of app runs, not globally (`launchctl getenv DYLD_INSERT_LIBRARIES` is empty); Wow.exe is byte-identical to before
- Check: `app.shim_scope`

#### Scenario: No performance cost
- **GIVEN** the real client launched by the play loop five times with the shim and five times without (same profile, stack already up)
- **WHEN** time-to-first-window and the game process's median CPU over 60 s at the login screen are measured
- **THEN** the shim's medians are within the run-to-run noise of the no-shim medians (startup within 2 s or 10%, CPU within 5 percentage points or 10%, whichever is larger), with all samples recorded
- Check: `app.shim_perf`

#### Scenario: Quit from the Dock stops everything
- **GIVEN** the stand-in up
- **WHEN** the launcher is asked to quit
- **THEN** everything stops as in "Open, play, quit" (checked through the existing SIGTERM path of `app.launch_lifecycle`); the Dock's Quit menu item itself is [human-verify] (driving it needs Accessibility permission)
- Check: `app.launch_lifecycle`

#### Scenario: Zach plays from the app
- **GIVEN** a built app
- **WHEN** Zach plays a session
- **THEN** he sees one Hearthstone icon in the Dock; clicking it from another app or Cmd-Tabbing brings the game back; full screen still works; Dock Quit shuts everything down
- [human-verify] Dock appearance, Cmd-Tab, full screen and the Dock menu can't be observed without Screen Recording or Accessibility permission.

### Requirement: Loading window with stages
The launcher's loading window SHALL show the current stage and a progress bar, without waiting for AI players.

#### Scenario: Stages in order with monotonic progress
- **GIVEN** the stack down
- **WHEN** the app is opened with the stand-in client
- **THEN** stage events use only the documented ids, appear in the documented order (prepare, database, login, world, bots, account, game_setup, launching, ready), and overall progress never decreases, starting at 0 and ending at 1
- Check: `app.loading_stages`

#### Scenario: The world stage shows real progress
- **GIVEN** a cold worldserver start
- **THEN** at least three distinct world fractions strictly between 0 and 1 are emitted before the world is done, each matching the worldserver log line count at that time within one poll
- Check: `play.world_progress`

#### Scenario: Bots are shown but don't hold up the launch
- **GIVEN** the population profile
- **WHEN** the world is ready
- **THEN** the bots stage reports the database's online count and the target, and the launching stage starts without waiting for the bot count to reach the target
- Check: `app.loading_stages`

#### Scenario: A running server skips the server stages
- **GIVEN** the stack already up
- **WHEN** the app is opened
- **THEN** database, login and world are reported as skipped within 2 s of start
- Check: `app.loading_stages_warm`

#### Scenario: Failures name the stage
- **GIVEN** the world can't start
- **THEN** stage events stop at world, and the error dialog names "Loading the world" and the log path
- Check: `app.startup_failure`

#### Scenario: Terminal output stays readable
- **GIVEN** `./play` in a terminal
- **THEN** every existing `[play]` line is unchanged, `[stage]` lines parse as JSON, and the existing `play.*` checks pass
- Check: `play.stage_lines`

#### Scenario: Zach sees the loading screen
- **WHEN** Zach opens the app
- **THEN** the loading window reads clearly and the bar moves smoothly through the stages
- [human-verify] Look and smoothness are judged by a person.
