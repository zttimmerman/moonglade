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

## As built (B21, 2026-10-05)
- **Stages:** `harness/hslib/stages.py` (`Stages`: ids/labels, monotonic overall, weights from `env/run/boot-calibration.json` durations, world watcher thread polling `worldserver.out` every 0.5 s against the profile's last good line count, default 880). `stack.up(on_stage=)` reports database/login/world start/done and world `spawned` (after the log is truncated); `launch.ensure_world/prepare(stages=)` skips the three server stages when the stack is up, then bots (informational: `population.online_bots()` vs the running profile's MaxRandomBots; also attached as `bots` to every later event and refreshed every 2 s during launching), account, game_setup. `play.py` adds prepare, launching (until `launch.client_window()` finds a layer-0 window >= 300x200 owned by this prefix's Wine host; 120 s fallback for a windowless stand-in) and ready, and on a startup failure emits `failed` for the current stage and stores `stage` in `play-last.json`. `say()` now writes each line in one call (same text) so the watcher thread can't interleave.
- **Launcher:** the loading window shows the stage label (world: plus its own %), a determinate bar for the whole start (animated, creeps within indeterminate stages toward 90 % of their slot, never backward), the latest `[play]` line (the bots stage heading reads "AI players: N / M online"; no separate bots line, Zach 2026-10-05). It no longer hides at "The game is starting"; it hides at the hand-off. The error dialog starts with "It stopped at: <stage label>."
- **Shim:** `harness/app/dockshim.m` -> `env/dockshim/hsdock.dylib` (universal x86_64+arm64) built by `harness/hslib/dockshim.py` from `hs app` (and on demand). `launch.wine_env(client=True)` (used only by `start_client`) adds `DYLD_INSERT_LIBRARIES` + `HSDOCK_*`; every other Wine call strips them. Policy: setting `dock_policy` (`accessory` default | `regular` | `off`; `HSDOCK_POLICY` env overrides; `hs dock-policy`), forced to `regular` outside the app. Log: `env/logs/dockshim.log`. After Wine's first `transformProcessToForeground:` the shim posts `local.hearthstone.window` (pid) so the launcher hands off at once; the play loop's `ready` stage is the fallback.
- **Test-only switches:** `HS_APP_TEST_DISABLE` (handoff, forward, reopen_note) and `HS_APP_SNAPSHOT_DIR` are read by the launcher only with the test hook; `HSDOCK_TEST`/`HSDOCK_TEST_*` (minimise notification, deliberate flicker) reach the shim only with `--client-cmd`, and `./play` refuses them otherwise.
- **Checks:** `harness/hslib/checks/polish_checks.py`. The app Dock/loading checks share one app session (population profile, notepad); the play checks share one terminal session (m0, notepad). `app.shim_perf` is slow tier (~25 min, real client, 5+5 interleaved runs).
