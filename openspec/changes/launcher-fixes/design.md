# Design: Launcher fixes
- Reopen: launcher.m reopen/become-active handler: if the game window isn't up yet, bring the loading window to the front (activate self, order front). Otherwise forward to the game as today. "Already running" only applies to a second *launch* (exit code 2 path).
- Shutdown window: the play loop emits shutdown stages (`[stage]` ids, e.g. shutdown: stopping game → saving world → stopping database → done); the launcher swaps the loading window into shutdown mode and closes when the play loop exits.
- Resolution:
  - Who owns the value: a marker of the launcher's last auto-chosen value (e.g. in env/run/ or a play-client side file). If `Config.wtf`'s `gxResolution` equals the marker (or is absent), the launcher may set it; if it differs, the player chose it, so leave it alone.
  - Candidates must be a size the client accepts. Investigate how 3.3.5 under winemac lists modes and whether windowed mode accepts arbitrary sizes; note `gxWindow=1` (windowed) is our default, and how macOS full screen interacts with it (the stretch may come from a fixed-size window being scaled).
  - Measurement: launch the real client at native and half-size several times at the same scene; record CPU %, GPU "Device Utilization %" (ioreg AGXAccelerator PerformanceStatistics, no permission needed), and frame time if obtainable. Decision rule: native is the default if its CPU and frame time are within 10% of half-size and GPU busy stays under ~80%; otherwise half-size. Record the numbers and the decision here.
- F25: add to the shared Continue run (fresh scratch client): no authserver login between client start and the Continue action.
- F26: extend GUI_PATHS with procs.py, paths.py, util.py, config.py, runner.py, frontmenu.py, clientbuild.py (or a dependency-based rule).
