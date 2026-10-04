# Proposal: Launcher polish (one Dock icon, staged loading screen)

## Why
Zach's play test of Hearthstone.app (2026-10-04): two Dock icons (the launcher and Wine's "wine"), and clicking Hearthstone says "already running" instead of showing the game. He liked the loading window and asked for a real progress indicator with the current stage, like Guild Wars 2's launcher. He moved this ahead of M4.

Research: `docs/research/launcher-polish-spike.md`. Decision: ADR-0010 (accepted; Wine host shim, option A), conditional on no performance impact.

## What Changes
1. **One Dock icon (ADR-0010, option A).**
   - The shim (x86_64 dylib, built by `hs app`) makes the game's Wine host process an accessory app named "Hearthstone" with our icon, so it has no Dock icon of its own.
   - The launcher hands focus to the game when its window appears, and forwards Dock/Finder clicks and Cmd-Tab activation to the game.
   - Clicking Hearthstone while the game is minimised restores it.
   - Quit from the Dock shuts down as today.
   - From `./play` in a terminal: name + icon only, so the game keeps a normal Dock icon.
   - The setting `HSDOCK_POLICY = accessory | regular` gives option B as a fallback.
2. **Loading window with stages.**
   - The play loop emits `[stage] {json}` lines: prepare, database, login, world, bots, account, game_setup, launching, ready.
   - The launcher shows a progress bar plus the stage label. The world stage shows real progress from the server log; bots show "AI players: N / M online".
   - Loading **doesn't wait for bots** (Zach, 2026-10-04): the game launches when the world is ready, and the count is informational.
   - The overall bar is weighted by the last run's stage timings and never moves backward. Server stages are skipped when the server is already running.
   - Failures name the stage in the error dialog.
3. **No performance regression:** startup time and CPU are measured with vs without the shim.

## Capabilities
### Modified Capabilities
- `mac-app`: one Dock icon with focus forwarding; staged loading window.
- `play-launcher`: emits stage lines (existing `[play]` output unchanged).

## Impact
New `harness/app/dockshim.m` (or similar), launcher.m changes, `launch.wine_env()` sets the shim only for app runs, play.py stage emission, new checks. No change to Wow.exe, `client/`, the pinned runtime directory, or the server.

## Spec-change log
(none yet)
