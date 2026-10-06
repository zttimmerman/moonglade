# Spec Delta: mac-app

## ADDED Requirements

### Requirement: Reopen always brings Hearthstone forward
Opening or clicking Hearthstone SHALL bring forward whatever Hearthstone is showing: the loading window while loading, the game after.

#### Scenario: Click during loading
- **GIVEN** the app opened and its loading window up, with another app frontmost
- **WHEN** the app is reopened through LaunchServices before the game window appears
- **THEN** within 2 s the loading window is frontmost, no "already running" dialog is shown, and loading continues to the game
- Check: `app.reopen_during_loading`

### Requirement: Shutdown is visible
After the game exits or the player quits from the Dock, the launcher SHALL show a shutdown window until everything has stopped, then exit.

#### Scenario: Quit shows the shutdown window until the stack is down
- **GIVEN** the game running from the app
- **WHEN** the launcher is asked to quit (or the game exits)
- **THEN** a shutdown window ("Saving and shutting down the world…", advising not to force-quit) is shown from the start of shutdown until the stack is down, its stages are recorded in order, the app exits 0 after the stack is down, and nothing remains running
- Check: `app.shutdown_window`

#### Scenario: Zach sees the shutdown window
- **WHEN** Zach quits from the Dock
- **THEN** the shutdown window explains what's happening until the app closes
- [human-verify] Look and clarity are judged by a person.

## MODIFIED Requirements

### Requirement: Double-click plays the game
Opening the app SHALL run the same play loop as `./play`: bring up the stack, launch the client when ready, and shut everything down cleanly when the client exits, without a terminal window.

#### Scenario: Open, play, quit
- **GIVEN** nothing from this repo is running
- **WHEN** the app is opened (`open dist/Hearthstone.app`, with the harness's stand-in client)
- **THEN** a "starting" status is shown, the stack reaches readiness, the client is launched, and when the client exits the stack shuts down gracefully; the app process exits 0, and no stack, Wine or Wow.exe process from this repo remains, with ports 3307/3724/8085/7878 free
- Check: `app.launch_lifecycle`

#### Scenario: Startup failure is shown, not hidden
- **GIVEN** the stack can't reach readiness
- **WHEN** the app is opened
- **THEN** it never starts the client, shows an error dialog that names the problem and the log path, exits nonzero, and leaves nothing running
- Check: `app.startup_failure`

#### Scenario: Second open while running
- **GIVEN** the app (or `./play`) is already running
- **WHEN** a second instance of the app is launched (`open -n`), or the app is opened while `./play` runs from a terminal
- **THEN** it shows an "already running" message, starts no second stack or client, and the first session continues unaffected; opening or clicking the *running* app instead brings forward whatever it shows (see "Reopen always brings Hearthstone forward")
- Check: `app.single_instance`

#### Scenario: Works after being moved
- **GIVEN** a built app copied to a different folder (outside `dist/`)
- **WHEN** the copy is opened
- **THEN** it finds the repo and plays exactly as in "Open, play, quit"
- Check: `app.relocatable`

#### Scenario: Zach opens it from Finder or the Dock
- **GIVEN** a built app
- **WHEN** Zach double-clicks it in Finder or clicks it in the Dock
- **THEN** they see the logo, the game starts without a terminal, and quitting the game leaves nothing running
- [human-verify] Finder/Dock interaction, first-run macOS prompts, and how the icon looks can only be judged by a person.
