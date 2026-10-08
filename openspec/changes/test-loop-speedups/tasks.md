## 1. Build (B35)
- [ ] 1.1 GUI preflight follows the GUI rule — verify: `report.gui_preflight`
- [ ] 1.2 Runs keep the display awake; `hs keep-awake` — verify: `report.keep_awake`
- [ ] 1.3 Last full run kept; timing-baseline join defaults to it — verify: `report.last_full_run`
- [ ] 1.4 Orphans of a killed run healed — verify: `report.orphans_healed`
- [ ] 1.5 `hs nightly` (+ --install/--uninstall, not installed by agents) — verify: `report.nightly`

## 2. Build (B36)
- [ ] 2.1 Full run under 25 minutes (safe concurrency) — verify: `report.full_run_wall`

## 3. Verify
- [ ] 3.1 Verifier pass + full harness; `hs timing-baseline join` on the new full run
- [ ] 3.2 (pending: Zach) Decide whether to install the nightly LaunchAgent
