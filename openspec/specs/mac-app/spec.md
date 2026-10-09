# mac-app Specification

## Purpose
A double-clickable Moonglade.app that takes the player from nothing running to in-game and back, using the project's own icon, with no terminal.

## Requirements

### Requirement: Double-click plays the game
Opening the app SHALL run the same play loop as `./play`: bring up the stack, launch the client when ready, and shut everything down cleanly when the client exits, without a terminal window.

#### Scenario: Open, play, quit
- **GIVEN** nothing from this repo is running
- **WHEN** the app is opened (`open dist/Moonglade.app`, with the harness's stand-in client)
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
- **THEN** it shows an "already running" message, starts no second stack or client, and the first session continues unaffected; opening or clicking the *running* app instead brings forward whatever it shows (see "Reopen always brings Moonglade forward")
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

### Requirement: One Moonglade in the Dock
When the game runs from Moonglade.app, the Dock SHALL show exactly one icon for it, Moonglade's, from launch to quit, and activating Moonglade by any route SHALL bring the game forward (ADR-0010).

#### Scenario: The game has no Dock icon of its own
- **GIVEN** the app opened with the GUI stand-in client (Wine's notepad under the same runtime and shim)
- **WHEN** the stand-in's window is up
- **THEN** `lsappinfo` reports the stand-in pid as a UIElement (no Dock icon) named Moonglade, the launcher pid as the only Foreground process of this session with bundle id `local.moonglade.app`, and no other process of this prefix as Foreground
- **AND** sampling every process of this prefix at least 10 times per second from the Wine host's start until 5 s after the window is up, none is ever reported as Foreground (no Wine Dock icon flashes, even briefly), with the sample count recorded
- Check: `app.single_dock_icon`

#### Scenario: Hand-off when the game window appears
- **GIVEN** the app opened
- **WHEN** the stand-in's first window appears
- **THEN** the loading window is hidden and the stand-in becomes frontmost within 2 s, without user input
- Check: `app.handoff_focus`

#### Scenario: Clicking Moonglade brings the game forward
- **GIVEN** the stand-in window is up and another app is frontmost
- **WHEN** the app is reopened through LaunchServices (as a Finder or Dock click does)
- **THEN** within 2 s the stand-in is frontmost, no "already running" dialog is shown, and the launcher keeps its single Dock icon
- Check: `app.reopen_focuses_game`

#### Scenario: A minimised game comes back on click
- **GIVEN** the stand-in window minimised (via a test-only shim notification)
- **WHEN** the app is reopened
- **THEN** the window is on screen and frontmost again
- Check: `app.reopen_unminimizes`

#### Scenario: Terminal ./play keeps a normal Dock icon with Moonglade's identity
- **GIVEN** `./play` from a shell with the GUI stand-in (no app)
- **WHEN** the window is up
- **THEN** the stand-in is a normal Foreground app named Moonglade, and the shim log records the icon replacement
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
- **THEN** he sees one Moonglade icon in the Dock; clicking it from another app or Cmd-Tabbing brings the game back; full screen still works; Dock Quit shuts everything down
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

### Requirement: Reopen always brings Moonglade forward
Opening or clicking Moonglade SHALL bring forward whatever Moonglade is showing: the loading window while loading, the game after.

#### Scenario: Click during loading
- **GIVEN** the app opened and its loading window up, with another app frontmost
- **WHEN** the app is reopened through LaunchServices before the game window appears
- **THEN** within 2 s the loading window is frontmost, no "already running" dialog is shown, and loading continues to the game
- Check: `app.reopen_during_loading`

### Requirement: Shutdown is visible
After the game exits or the player quits from the Dock, the launcher SHALL show a shutdown window until everything has stopped, then exit. On a genuine player session the stages SHALL include "Backing up your save" after the login server stops and before the database stops.

#### Scenario: Quit shows the shutdown window until the stack is down
- **GIVEN** the game running from the app
- **WHEN** the launcher is asked to quit (or the game exits)
- **THEN** a shutdown window ("Saving and shutting down the world…", advising not to force-quit) is shown from the start of shutdown until the stack is down, its stages are recorded in order, the app exits 0 after the stack is down, and nothing remains running
- Check: `app.shutdown_window`

#### Scenario: The backup stage is shown in order
- **GIVEN** the game running from the app with a scratch backup folder (test switches)
- **WHEN** the game exits
- **THEN** the recorded shutdown stages include `shutdown_backup` after `shutdown_login` and before `shutdown_database`, with the window visible during it, and a new backup exists in the scratch folder
- Check: `app.shutdown_window`

#### Scenario: Zach sees the shutdown window
- **WHEN** Zach quits from the Dock
- **THEN** the shutdown window explains what's happening until the app closes
- [human-verify] Look and clarity are judged by a person.

### Requirement: The app is named Moonglade
The app SHALL be `Moonglade.app` with bundle identifier `local.moonglade.app`, and every user-visible name (bundle name, Dock and Cmd-Tab name, launcher windows and dialogs, game-process display name) SHALL be "Moonglade".

#### Scenario: Built bundle carries the new name
- **GIVEN** a clean build via `harness/hs app`
- **WHEN** the bundle is inspected
- **THEN** `dist/Moonglade.app` exists with CFBundleName/CFBundleDisplayName "Moonglade", bundle id `local.moonglade.app` and designated requirement `identifier "local.moonglade.app"`, and no old project name appears in the launcher executable, Info.plist or shim
- Check: `app.bundle_valid`

#### Scenario: Running app shows the new name
- **GIVEN** a built app
- **WHEN** it is opened with the stand-in client
- **THEN** the launcher's and the game process's LaunchServices display names are "Moonglade", and no user-visible launcher text says "Hearthstone"
- Check: `app.names_running`

### Requirement: Option-click opens Backups
Opening Moonglade with the Option key held SHALL show a Backups window instead of starting the game. It lists every backup, newest first, with date and time, kind (automatic or before restore) and the player's characters (name, level, class); offers Restore with a confirmation dialog that says what will be rewound and that a safety backup is taken first; and afterwards offers Play or Quit. A notice that the last backup failed SHALL appear in this window and in the loading window.

#### Scenario: Option-open lists backups and starts nothing
- **GIVEN** a built app, a scratch backup folder with 3 backups, and the Option key reported held at launch (test-injected through the app's test hook)
- **WHEN** the app is opened
- **THEN** the Backups window is shown listing the 3 backups newest first with the manifest's date, kind and characters; no stack process, Wine process or client is started; and closing the window exits the app 0 with nothing running
- Check: `app.backups_window`

#### Scenario: Restore from the window, then play
- **GIVEN** the Backups window from the previous scenario
- **WHEN** a backup is chosen and Restore is confirmed (test-injected), then Play is chosen
- **THEN** the confirmation text names the backup's date and says a safety backup is taken first; the restore runs (a safety backup appears and the databases match the chosen backup's checksums); and the normal loading flow then starts the stand-in client
- Check: `app.backups_window`

#### Scenario: Cancelling changes nothing
- **GIVEN** the Backups window
- **WHEN** Restore is chosen and the confirmation is cancelled (test-injected)
- **THEN** no safety backup is taken and every table checksum is unchanged
- Check: `app.backups_window`

#### Scenario: Zach opens Backups
- **WHEN** Zach holds Option and clicks Moonglade in the Dock or Finder
- **THEN** the Backups window is clear about what each backup is and what restoring does
- [human-verify] Clarity and the real Option-key path (not injected) are judged by a person.

### Requirement: Friendly launcher errors
The launcher's error dialogs SHALL state the problem in plain words, with no server/database/port wording in the main text, and offer **Show details** that reveals the technical error and the log path.

#### Scenario: Startup failure is friendly, with details
- **GIVEN** a built app and a stack that can't reach readiness (the existing startup-failure setup)
- **WHEN** the app is opened
- **THEN** the dialog's main text has none of the words server, database, MySQL, port, realm or SOAP; the details text names the technical error and the log path; and the app exits nonzero with nothing running (as before)
- Check: `app.startup_failure`

### Requirement: Restore confirms when it's done
After a restore from the Backups window, the window SHALL say that the restore finished and which save is now in place ("Restored. Your save is now from <date>."), or that it failed and why.

#### Scenario: Restore shows a done message
- **GIVEN** the Backups window with a scratch backup folder (test hook)
- **WHEN** a restore is confirmed and completes, and separately a restore fails (damaged backup)
- **THEN** the window shows the done message naming the backup's date after success, and a failure message naming the reason after failure, before Play or Quit is chosen
- Check: `app.backups_window`

### Requirement: App bundle with the navy-tile icon
`harness/hs app` SHALL build `dist/Moonglade.app`, a valid, ad-hoc-signed macOS app bundle whose icon is Zach's logo on a dark navy macOS rounded-square tile (Zach, 2026-10-08) whose outline is macOS 26's app-icon shape: an 832 px body on the 1024 px canvas with continuous-curvature corners of radius 216 px (Zach picked option C, 2026-10-09). Its Info.plist SHALL carry no "not for redistribution" (or other "redistribution") wording: the source is public and releases are intended (ADR-0012).

#### Scenario: Bundle is valid and signed
- **GIVEN** a clean checkout with the committed branding assets
- **WHEN** `harness/hs app` runs
- **THEN** `dist/Moonglade.app` exists; `plutil -lint` passes on its Info.plist; the plist names the executable, the bundle identifier and the icon, and no value in it contains "redistribut" (any case); the executable is present and executable; `codesign --verify --deep --strict` passes; and nothing outside the repo was written
- Check: `app.bundle_valid`

#### Scenario: Icon is the logo on a navy tile
- **GIVEN** the committed `AppIcon.icns` and its PNG
- **WHEN** the harness decodes them
- **THEN** the .icns contains every standard size from 16 to 1024 px (incl. @2x); in the 1024 px image, with the tile's geometry taken from AppIcon.json's `tile` record (never hard-coded in the check), the canvas corners and every pixel outside the tile's rounded square and its recorded shadow band are fully transparent (alpha 0); a ring of sample points just inside the tile's edge, including its four rounded corners, is opaque and within a small colour distance of the recorded navy; the centre is opaque; the gold ring is present at the recorded scale (sampled points on the ring are gold, not navy); and re-running the generator on the logo source reproduces the committed PNG pixel for pixel. A scratch icon rendered with the old transparent layout, and one with a white tile, each fail
- Check: `app.icon`

#### Scenario: The tile has macOS 26's continuous-corner shape
- **GIVEN** the committed `AppIcon.png` and AppIcon.json's `tile` record
- **WHEN** the harness reads the record and renders the tile mask with the generator
- **THEN** the record names a continuous-corner shape with body 832 px and corner radius 216 px, centred on the 1024 px canvas (x0 = y0 = 96 px), and the logo diameter shown in Zach's approved preview (unchanged from the circular-arc tile); the generator's mask matches a Core Animation `cornerCurve = continuous` rendering of the same body and radius to within 1/255 mean and 64/255 at any pixel (anti-aliasing only), and a mask whose corners are circular arcs of the same radius fails that comparison; the committed PNG's tile-edge samples (incl. the corners at 45°) lie inside the continuous outline, not the circular one
- Check: `app.icon`

### Requirement: One icon everywhere
The dev app, the release app and the running game's Dock icon (the Wine host shim's `HSDOCK_ICON`, dev and release, app and terminal runs) SHALL show the same navy-tile icon, the one generated into `branding/`.

#### Scenario: Every icon file decodes to the same pixels
- **GIVEN** the built dev app, the packaged release app (when this run packaged one; otherwise its fixture icon from `release.py`'s app wrapper on a scratch app) and the dock environment that `launch.dock_env()` produces for a dev app run, a dev terminal run and a release app run
- **WHEN** each icon file (the two bundles' `CFBundleIconFile`, each `HSDOCK_ICON` path) is decoded at 1024 px
- **THEN** all decode to identical pixels, equal to `branding/AppIcon.png`, and the tile-edge samples are navy; a scratch environment whose `HSDOCK_ICON` points at an icon with a transparent surround fails
- Check: `app.icon_everywhere`

#### Scenario: The running game's Dock icon is the navy tile
- **GIVEN** the dev app opened with the GUI stand-in client under the shim (test mode)
- **WHEN** the shim replaces the icon Wine passes to `setApplicationIconImage:`
- **THEN** the shim's log from this run (newer than the run's start) records that it loaded the icon and the colours it sampled from the loaded image at the tile-edge and corner points, and these match navy and transparent; a counterexample run with `HSDOCK_ICON` pointing at a transparent-surround icon logs non-navy samples and fails
- Check: `app.dock_icon_rendered`

#### Scenario: Zach approves the look
- **GIVEN** a preview sheet of the icon at 16, 32, 128, 512 and 1024 px on light and dark backgrounds, and the rebuilt dev app
- **WHEN** Zach looks at it, and at the app in Finder, the Dock and Cmd-Tab on macOS 26 while the game runs
- **THEN** he approves the navy, the logo size, and that macOS shows no extra plate around the tile
- [human-verify] How the icon looks, and whether macOS 26 adds a plate, is a person's judgement; Zach asked to see a preview before it ships (2026-10-08).
