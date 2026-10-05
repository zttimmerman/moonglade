# Design: Launcher fixes
- Reopen: launcher.m reopen/become-active handler: if the game window isn't up yet, bring the loading window to the front (activate self, order front). Otherwise forward to the game as today. "Already running" only applies to a second *launch* (exit code 2 path).
- Shutdown window: the play loop emits shutdown stages (`[stage]` ids, e.g. shutdown: stopping game → saving world → stopping database → done); the launcher swaps the loading window into shutdown mode and closes when the play loop exits.
- Resolution:
  - Who owns the value: a marker of the launcher's last auto-chosen value (e.g. in env/run/ or a play-client side file). If `Config.wtf`'s `gxResolution` equals the marker (or is absent), the launcher may set it; if it differs, the player chose it, so leave it alone.
  - Candidates must be a size the client accepts. Investigate how 3.3.5 under winemac lists modes and whether windowed mode accepts arbitrary sizes; note `gxWindow=1` (windowed) is our default, and how macOS full screen interacts with it (the stretch may come from a fixed-size window being scaled).
  - Measurement: launch the real client at native and half-size several times at the same scene; record CPU %, GPU "Device Utilization %" (ioreg AGXAccelerator PerformanceStatistics, no permission needed), and frame time if obtainable. Decision rule: native is the default if its CPU and frame time are within 10% of half-size and GPU busy stays under ~80%; otherwise half-size. Record the numbers and the decision here.
- F25: add to the shared Continue run (fresh scratch client): no authserver login between client start and the Continue action.
- F26: extend GUI_PATHS with procs.py, paths.py, util.py, config.py, runner.py, frontmenu.py, clientbuild.py (or a dependency-based rule).

## Resolution investigation (B23, 2026-10-05)
All on this Mac (built-in 3024x1964 Retina panel, "looks like" 1512x982, camera housing at the top), with a scratch
play client (env/check-play-res), the real Wow.exe under the mtld3d layer, `gxWindow 1`. Throwaway probes in
env/tmp/resinv/ (gitignored): a tiny Windows program that lists the modes Wine and mtld3d report, and an
investigation-only dylib that logs the Wine window's geometry and toggles macOS full screen.

**What the client is offered.**
- Wine (winemac, Retina mode off, the default) reports the desktop in points: `GetSystemMetrics` 1512x982, current
  mode 1512x982, plus a list of 22 sizes up to 3024x1964 (`EnumDisplaySettings`).
- mtld3d's D3D9 adapter (what the client's Video options list) reports 10 modes: 3024x1964 (the panel in pixels),
  2880x1800, 2560x1600, 1920x1200, 1680x1050, 1440x900, 1280x800, 1024x768, 800x600, 640x480. 1512x982 isn't one.

**What the client does with `gxResolution` in windowed mode.** It opens a window with the value's aspect ratio,
shrunk to fit the visible screen (1512x876 pt with the Dock), and its back buffer is that window (mtld3d
`present: guest WxH, layer WxHpt @1.00x`). Unlisted sizes work the same way:

| gxResolution | window content (pt) | back buffer | aspect |
|---|---|---|---|
| (none) | 1024x768 | 1024x768 | 1.333 |
| 1512x982 | 1144x743 | 1144x743 | 1.540 |
| 3024x1964 | 1295x841 | 1295x841 | 1.540 |
| 1440x935 | 1229x798 | 1229x798 | 1.540 |
| 1512x949 | 1179x740 | 1179x740 | 1.593 |
| 3024x1898 | 1340x841 | 1340x841 | 1.593 |

The Metal layer is at scale 1.00 (Wine's Retina mode is off), so the macOS compositor scales it 2x on this panel:
the client never renders at true Retina resolution here, whatever `gxResolution` says.

**macOS full screen and the stretch.** The window is Wine's, with full-screen support (collection behaviour 0xa4).
In full screen its content becomes **1512x949 pt**: the panel minus the 33 pt strip beside the camera housing
(`NSScreen.safeAreaInsets.top` = 32). The client doesn't reset its device, so mtld3d keeps the back buffer and
scales the frame to the window: the default 1024x768 (4:3) frame is drawn 19.5 % too wide (1.593/1.333): "night elf
wide". A back buffer with the full panel's ratio (3024x1964, 1.540) would still be 3.4 % too wide on this Mac, so the
launcher matches the **full-screen area** (screen minus the top safe-area inset), which is what "the display" means
for the 2 % rule here. On a display without a camera housing the two are the same.

**The client keeps the value.** A clean quit through the menu (windowed, and after full screen) leaves
`gxResolution` exactly as written, so the ownership marker stays valid across sessions.

## Native vs half-size measurement (B23, 2026-10-05)
Login screen, macOS full screen, scratch play client, real Wow.exe, 3 runs each, interleaved, screen unlocked and
awake (`caffeinate -d -i`), 8 s settle then 60 s: Wow.exe CPU % every 2 s (`ps` time), GPU "Device Utilization %"
from `ioreg -c AGXAccelerator` PerformanceStatistics. "Native" = the full-screen area in Retina pixels (3024x1898),
"half" = in points (1512x949).

| run | value | back buffer | Wow.exe CPU % median (min-max) | GPU busy % median (min-max) |
|---|---|---|---|---|
| m0 | 3024x1898 | 1340x841 | 73.2 (70.9-98.9) | 27 (26-27) |
| m0 | 1512x949 | 1179x740 | 73.5 (71.5-99.2) | 27 (26-27) |
| m1 | 1512x949 | 1179x740 | 74.0 (72.0-98.3) | 27 (26-28) |
| m1 | 3024x1898 | 1340x841 | 73.7 (71.9-99.6) | 27 (26-27) |
| m2 | 3024x1898 | 1340x841 | 73.5 (72.5-98.2) | 27 (26-27) |
| m2 | 1512x949 | 1179x740 | 73.75 (72.0-99.2) | 26.5 (26-28) |

Medians of the run medians: native CPU 73.5 %, half 73.75 % (-0.3 %); GPU 27 % for both.
Frame time: no client-side counter is readable. The client's frame-rate display is an in-game overlay (Ctrl-R);
mtld3d logs no frame statistics; the Metal HUD (`MTL_HUD_ENABLED=1`, `MTL_HUD_LOG_ENABLED=1`) loaded into the Wine
host but wrote no frame lines to the unified log. The rule's frame-time clause is therefore unmeasured (vsync is on
at 120 Hz in both cases).

Why they cost the same: in both cases the back buffer is the window the client fits on screen (1340x841 vs
1179x740), not the value itself, and the GPU is far from busy.

**Decision: default = native** (rule: CPU within 10 % (−0.3 %), GPU busy under 80 % (27 %); frame time not readable,
recorded above). On this Mac the launcher writes `gxResolution 3024x1900` (1512x950 pt full-screen area x 2; the
1 pt difference to the measured 949 is 0.1 % in aspect). Native also gives the slightly larger back buffer
(sharper after scaling). A true Retina-resolution picture would need Wine's Retina mode (a prefix-wide setting with
UI-scaling side effects), out of scope here.

## As built (B23)
- **Resolution:** `harness/hslib/resolution.py`; `launch.prepare` calls `resolution.apply(client_dir())` after the
  client config, for play clients only (never client/), and records it as `prepare.resolution` in play-last.json.
  Display: `NSScreen.mainScreen` via JXA (points, backingScaleFactor, safeAreaInsets.top). Ownership marker:
  `<play client>/WTF/hs-resolution.json` (the value the launcher last wrote; WTF/ survives play-client rebuilds).
  Absent value or value == marker: the launcher may (re)write; anything else is the player's and is left alone.
  The marker is written whole (tmp + rename), first with the old and the pending value, then the new one, so a kill
  between the marker and Config.wtf writes still leaves both values recognised as ours.
  Harness runs with the test account on the player's own env/client-play skip it (`prepare(set_resolution=False)`,
  recorded as skipped): only the player's own launches, or a scratch play client, get the default. (Found when a
  builder run of app.single_instance wrote it into env/client-play; that write was reverted.)
  An error reading the display never blocks a launch (recorded, no new [play] line). Test-only `$HS_TEST_DISPLAY`
  (scratch play client only; ./play refuses it without --allow-test-client-dir).
- The login checks' marker reader (`playclient_util.marker`) now scales with the screenshot height (UI = 768 units),
  since scratch play clients get the default resolution too.
- **Reopen:** `applicationShouldHandleReopen:` forwards to the game once handed off (as before); otherwise, while the
  play loop runs (loading, or shutting down), it activates the launcher and orders the loading/shutdown window
  front, recording `{"kind":"reopen","showing":"loading"|"shutdown"}`. The "already running" dialog stays only on
  the second-launch path (play loop exit code 2).
- **Shutdown:** `[shutdown] {json}` lines (own prefix, so the `[stage]` loading protocol and its checks are
  unchanged), ids shutdown_game, shutdown_world, shutdown_login, shutdown_database, shutdown_done (stages.py
  `Shutdown`; `stack.down(on_stage=)`). The launcher enters shutdown mode on a quit request (immediately) or the first
  `[shutdown]` line, retitles the window "Saving and shutting down the world…" with "Please don't force-quit…",
  shows it with `orderFrontRegardless` (on top, no focus taken), and closes it when the play loop has exited.
  Hook events: `shutdown_window` (shown/closed, with AppKit `visible` and window-server `onscreen`),
  `shutdown_stage` per line. Test switches (test hook only): loading_reopen, loading_reopen_hide, shutdown_window,
  shutdown_early_close.
- **Checks:** `app.reopen_during_loading` (GUI tier; a step of the shared focus session, during the world stage) and
  `app.shutdown_window` (full tier, not GUI: its assertions read hook events and a stack sampler; the window is
  ordered front without activation). The shared app session now ends with a quit request as from the Dock
  (`NSRunningApplication -terminate`) instead of the stand-in exiting (app.launch_lifecycle still covers the game
  exiting). `client.resolution_default` (full tier; real client in part B). `login.fresh_menu_waits` (F25) on the
  shared Continue run, which gains a screenshot at 17 s (before the Continue at 20 s; the file lands up to ~1 s after the act, so 3 s of margin). F26: GUI_PATHS gains
  play_checks.py, procs.py, paths.py, util.py, config.py, runner.py, frontmenu.py, clientbuild.py, clientcfg.py,
  resolution.py.
- `app.single_instance` (a) now opens a second *instance* (`open -n`): a plain reopen during loading no longer shows
  "already running" (this change's requirement), so "already running" is exercised where it still applies, the
  second launch.

## Dynamic on any display (B23, follow-up after Zach, 2026-10-05)
- Nothing is fixed to this Mac: at each launch `resolution.display()` reads the display the game will open on, via
  `NSScreen.screens[0]`: size in points, `backingScaleFactor`, `safeAreaInsets.top` (0 without a camera housing).
  Value = (width, height − top inset) × scale (the native decision). Examples, from client.resolution_default's
  cases: notched 14" 1512x982@2 inset 33 → 3024x1898; 16:10 laptop 1440x900@2 → 2880x1800; external 1920x1080@1
  → 1920x1080; ultrawide 3440x1440@1 → 3440x1440; 2560x1440 at scale 2 → 5120x2880, at scale 1 → 2560x1440.
- **What the client takes:** the real client was given 1024x768 up to 7680x4320 (also 6880x2880, 6016x3384,
  3440x1440); it always opened a window of the value's shape that fits the screen, e.g. 6880x2880 → 1504x630
  (aspect 2.387), so no size cap is needed. The check's "client takes it" range is 640x480 to 7680x4320, and its end-to-end
  part runs the real client at 6880x2880 (ultrawide at scale 2) and 1920x1080 (16:9 at scale 1).
- **Several displays:** Wine's primary monitor is the macOS primary display (`CGMainDisplayID`, the one with the
  menu bar = `NSScreen.screens[0]`) and the client opens its window there, so that is the display read. Not
  `NSScreen.mainScreen`: that's whichever screen has keyboard focus. Moving the game window to another display and
  going full screen there uses that display's shape, which the launcher doesn't follow mid-session; set the
  primary display in System Settings > Displays, or pick a resolution in the game. (Single-display Mac here, so the
  multi-display choice is from Wine's documented behaviour, not tested on hardware.)
- **The display changes between launches** (another primary display, a monitor plugged in, a scaling change): the
  next launch reads it again and, if the value is still the launcher's own (the marker), re-picks it; a value the
  player set is never touched.

## Dock sampler (B23, follow-up; test-only)
app.single_dock_icon's `LsSampler` was a `lsappinfo list` subprocess per sample (two staggered threads); under CPU
load it left gaps of 0.30-0.39 s (limit 0.25 s). It is now one long-lived helper, `harness/hslib/checks/lssampler.m`
(compiled on demand into env/tools/lssampler): every 25 ms it lists `NSWorkspace.runningApplications` for this runtime's
processes and our launcher, maps `activationPolicy` to lsappinfo's types (Foreground / UIElement / BackgroundOnly),
spins the run loop so the list and policies are current, and streams JSON lines. Same limits (>= 10 Hz, max gap
0.25 s) and the same Foreground-sample failure. With `yes > /dev/null` x2 running: 3/3 sessions at 36.2 / 36.5 /
36.8 Hz, max gaps 0.100 / 0.076 / 0.054 s; the 100 ms forced flicker (HSDOCK_TEST_FLICKER_MS=100) still fails
(20 Foreground samples).
