# Spec Delta: mac-app

## Purpose
A double-clickable Hearthstone.app that takes the player from nothing running to in-game and back, using the project's own icon, with no terminal.

## ADDED Requirements

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
