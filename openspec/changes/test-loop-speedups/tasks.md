## 1. Build (B35)
- [x] 1.1 GUI preflight follows the GUI rule — verify: `report.gui_preflight`
- [x] 1.2 Runs keep the display awake; `hs keep-awake` — verify: `report.keep_awake`
- [x] 1.3 Last full run kept; timing-baseline join defaults to it — verify: `report.last_full_run`
- [x] 1.4 Orphans of a killed run healed — verify: `report.orphans_healed`
- [x] 1.5 `hs nightly` (+ --install/--uninstall, not installed by agents) — verify: `report.nightly`

## 2. Build (B36)
- [x] 2.1 Measured: safe concurrency can't reach 25 min (requirement withdrawn, see the spec-change log); side lane reverted

## 3. Verify
- [ ] 3.1 Verifier pass + full harness; `hs timing-baseline join` on the new full run
- [ ] 3.2 (pending: Zach) Decide whether to install the nightly LaunchAgent
