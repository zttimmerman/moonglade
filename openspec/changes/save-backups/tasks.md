# Tasks
- [ ] 1.1 `backups.py`: take/list/verify/rotate, atomic writes, free-space guard; hook into `stack.down` + play loop; `shutdown_backup` stage — verify: `backups.on_quit`, `backups.rotation`, `backups.failure_nonblocking`, `backups.kill_safe`, `backups.player_untouched`
- [ ] 1.2 Restore + `./play --backups` CLI with refusals — verify: `backups.restore_roundtrip`, `backups.restore_refusals`
- [ ] 1.3 App: Option-click Backups window, failure notice, backup stage in the shutdown window — verify: `app.backups_window`, `app.shutdown_window`
- [ ] 1.4 Measure size/time, record in design.md; README/STATUS mention how to restore
- [ ] 2.1 Verifier pass + full harness
- [ ] 2.2 (pending: Zach) Human: Option-click Moonglade, restore an older backup, play
