## REMOVED Requirements

### Requirement: App bundle with the project icon
**Reason**: Zach chose a dark navy tile for the icon (2026-10-08), so "logo on a transparent background" no longer holds; a MODIFIED block can't drop its "Icon is the logo on a transparent background" scenario.
**Migration**: Replaced by "App bundle with the navy-tile icon" below, which keeps "Bundle is valid and signed" (tightened) and replaces the icon scenario with "Icon is the logo on a navy tile".

## ADDED Requirements

### Requirement: App bundle with the navy-tile icon
`harness/hs app` SHALL build `dist/Moonglade.app`, a valid, ad-hoc-signed macOS app bundle whose icon is Zach's logo on a dark navy macOS rounded-square tile (Zach, 2026-10-08). Its Info.plist SHALL carry no "not for redistribution" (or other "redistribution") wording: the source is public and releases are intended (ADR-0012).

#### Scenario: Bundle is valid and signed
- **GIVEN** a clean checkout with the committed branding assets
- **WHEN** `harness/hs app` runs
- **THEN** `dist/Moonglade.app` exists; `plutil -lint` passes on its Info.plist; the plist names the executable, the bundle identifier and the icon, and no value in it contains "redistribut" (any case); the executable is present and executable; `codesign --verify --deep --strict` passes; and nothing outside the repo was written
- Check: `app.bundle_valid`

#### Scenario: Icon is the logo on a navy tile
- **GIVEN** the committed `AppIcon.icns` and its PNG
- **WHEN** the harness decodes them
- **THEN** the .icns contains every standard size from 16 to 1024 px (incl. @2x); in the 1024 px image the pixels outside the tile's rounded square (the canvas corners and margin) are fully transparent (alpha 0); a ring of sample points just inside the tile's edge, including its four rounded corners, is opaque and within a small colour distance of the recorded navy; the centre is opaque; the gold ring is present at the recorded scale (sampled points on the ring are gold, not navy); and re-running the generator on the logo source reproduces the committed PNG pixel for pixel. A scratch icon rendered with the old transparent layout, and one with a white tile, each fail
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
