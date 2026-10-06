# Tasks
- [x] 1.1 `backups.py`: take/list/verify/rotate, atomic writes, free-space guard; hook into `stack.down` + play loop; `shutdown_backup` stage — verify: `backups.on_quit`, `backups.rotation`, `backups.failure_nonblocking`, `backups.kill_safe`, `backups.player_untouched`
- [x] 1.2 Restore + `./play --backups` CLI with refusals — verify: `backups.restore_roundtrip`, `backups.restore_refusals`
- [x] 1.3 App: Option-click Backups window, failure notice, backup stage in the shutdown window — verify: `app.backups_window`, `app.shutdown_window`
- [x] 1.4 Measure size/time, record in design.md; README/STATUS mention how to restore
- [x] 1.5 Backups tier (path rule, `--tier backups`, slow sweep) + timing guard excludes it — verify: `report.backups_tier_rule`, `report.timing`
- [x] 2.1 Verifier pass + full harness (cycle 2 PASS at c21526c: full 104 pass, report.timing 1780 s vs 1867.5 s; 4 GUI focus checks BLOCKED-ENV on a locked screen, then `--tier gui` 9/0 PASS on the unlocked screen 2026-10-06 16:44; cycle 1 FAIL on report.timing fixed by the backups tier)
- [ ] 2.2 (pending: Zach) Human: Option-click Moonglade, restore an older backup, play
