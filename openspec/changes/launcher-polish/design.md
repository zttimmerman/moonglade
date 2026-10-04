# Design: Launcher polish
See `docs/research/launcher-polish-spike.md` (Recommendation, option A; Feature 2) and ADR-0010.

## Key points
- Shim env: `HSDOCK_POLICY` (accessory|regular), `HSDOCK_NAME=Hearthstone`, `HSDOCK_ICON=<bundle>/Contents/Resources/AppIcon.icns`, plus `DYLD_INSERT_LIBRARIES=<shim>`. All set in `launch.wine_env()` only when the play loop runs under the app (the launcher passes a marker, e.g. `HS_APP=1`). Terminal `./play` gets name + icon only.
- Shim duties:
  - set the activation policy at startup and on first window;
  - set the display name and icon;
  - make Wine's `transformProcessToForeground:` idempotent;
  - on the `local.hearthstone.reopen` distributed notification, call Wine's reopen/unminimise path;
  - log what it did to a file under `env/logs/` for checks.
- Launcher:
  - stays Regular with the Hearthstone icon;
  - when the game's first window appears, hides the loading window and yields activation to the game (`yieldActivationToApplication:` + `activateFromApplication:`);
  - `applicationShouldHandleReopen:` and `applicationDidBecomeActive:` forward to the game and post `local.hearthstone.reopen`;
  - "already running" applies only to a second *launch* when no game window is ours to focus.
- Stages:
  - The play loop prints `[stage] {"id":…, "label":…, "state":"start|progress|done|skipped", "fraction":…, "overall":…}`.
  - The launcher shows the label + a determinate bar, and records each stage as a hook event.
  - World fraction = current `worldserver.out` line count vs the last good boot's count (stored).
  - Bots = DB online count vs target; informational only, doesn't gate launch.
  - Weights come from the last run's stage durations, stored in `env/run/`.
- Stand-in for GUI checks: Wine's built-in `notepad` under the same runtime + shim. Never Wow.exe in Dock checks.

## Risks
See ADR-0010 Consequences: lost Wine menu shortcuts, unverified native full screen (fallback `regular`), private API for the display name, runtime bumps re-run the checks.
