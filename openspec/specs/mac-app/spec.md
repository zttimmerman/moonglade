# mac-app Specification

## Purpose
A double-clickable Hearthstone.app that takes the player from nothing running to in-game and back, using the project's own icon, with no terminal.

## Requirements

### Requirement: App bundle with the project icon
`harness/hs app` SHALL build `dist/Hearthstone.app`, a valid, ad-hoc-signed macOS app bundle whose icon is Zach's logo on a transparent background.

#### Scenario: Bundle is valid and signed
- **GIVEN** a clean checkout with the committed branding assets
- **WHEN** `harness/hs app` runs
- **THEN** `dist/Hearthstone.app` exists; `plutil -lint` passes on its Info.plist; the plist names the executable, the bundle identifier and the icon; the executable is present and executable; `codesign --verify --deep --strict` passes; and nothing outside the repo was written
- Check: `app.bundle_valid`

#### Scenario: Icon is the logo on a transparent background
- **GIVEN** the committed `AppIcon.icns` and cleaned PNG
- **WHEN** the harness decodes them
- **THEN** the .icns contains every standard size from 16 to 1024 px (incl. @2x); in the 1024 px image the four corners and the area outside the ring are fully transparent (alpha 0), the centre is opaque, and no white surround (near-white opaque pixels outside the ring) remains; and re-running the generator on the source reproduces the committed PNG
- Check: `app.icon`

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
- **WHEN** the app is opened again
- **THEN** it shows an "already running" message, starts no second stack or client, and the first session continues unaffected
- Check: `app.single_instance`

#### Scenario: Works after being moved
- **GIVEN** a built app copied to a different folder (outside `dist/`)
- **WHEN** the copy is opened
- **THEN** it finds the repo and plays exactly as in "Open, play, quit"
- Check: `app.relocatable`

#### Scenario: Zach opens it from Finder or the Dock
- **GIVEN** a built app
- **WHEN** Zach double-clicks it in Finder or clicks it in the Dock
- **THEN** he sees the logo, the game starts without a terminal, and quitting the game leaves nothing running
- [human-verify] Finder/Dock interaction, first-run macOS prompts, and how the icon looks can only be judged by a person.

### Requirement: One Hearthstone in the Dock
When the game runs from Hearthstone.app, the Dock SHALL show exactly one icon for it, Hearthstone's, from launch to quit, and activating Hearthstone by any route SHALL bring the game forward (ADR-0010).

#### Scenario: The game has no Dock icon of its own
- **GIVEN** the app opened with the GUI stand-in client (Wine's notepad under the same runtime and shim)
- **WHEN** the stand-in's window is up
- **THEN** `lsappinfo` reports the stand-in pid as a UIElement (no Dock icon) named Hearthstone, the launcher pid as the only Foreground process of this session with bundle id `local.hearthstone.app`, and no other process of this prefix as Foreground
- **AND** sampling every process of this prefix at least 10 times per second from the Wine host's start until 5 s after the window is up, none is ever reported as Foreground (no Wine Dock icon flashes, even briefly), with the sample count recorded
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
- **THEN** `client.inspect` passes; the shim file lives outside `client/` and outside `env/wowsilicon-r17/` (whose pinned hash is unchanged); `DYLD_INSERT_LIBRARIES` is set only in the environment of the Wine host processes the play loop starts for the game (app runs: accessory policy; terminal `./play`: name and icon only), never for other processes and never globally (`launchctl getenv DYLD_INSERT_LIBRARIES` is empty); Wow.exe is byte-identical to before
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
