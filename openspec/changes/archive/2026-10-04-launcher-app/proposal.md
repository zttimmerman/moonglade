# Proposal: Launcher .app

## Why
North star: "offline WoW that feels like one single-player app". Today you start the game by running `./play` in a terminal. A double-clickable Mac app with Zach's logo is the first step toward the front menu, and the agreed next item after M3 (STATUS, front-menu vision).

## What Changes
- `harness/hs app` builds `dist/Hearthstone.app` from the repo: a small app bundle (Info.plist, launcher executable, icon) that runs the same play loop as `./play`. The bundle records the repo's absolute path at build time, so it keeps working when copied to /Applications or the Dock.
- Icon: Zach's "zW+" gold-ring logo (`env/branding/logo-source.jpeg`, 2048×2048 JPEG with a white surround, gitignored). A generator script masks it to a transparent circle and produces `branding/AppIcon.icns` (16–1024 px incl. @2x). The cleaned PNG and the .icns are committed; the JPEG stays local.
- Behaviour on double-click:
  - shows that it's starting (a progress notification/dialog while the server boots);
  - launches the game when the server is ready;
  - shuts everything down when the game closes;
  - surfaces failures in a dialog with the log path, never failing silently;
  - a second double-click while running brings up a "already running" message instead of a second world.
- No client binary changes; no server changes. Building the app never writes outside the repo (Zach drags it to /Applications himself if he wants).

## Capabilities
### New Capabilities
- `mac-app`: a double-clickable Hearthstone.app that runs the play loop with the project's icon.

### Modified Capabilities
(none) The app reuses the `play-launcher` loop as is.

## Impact
New `hs app` command, `harness/hslib/app.py`, branding assets and generator, new checks. No change to the stack, the client, or game data (CLAUDE.md §7 doesn't apply).

## Spec-change log
(none yet)
