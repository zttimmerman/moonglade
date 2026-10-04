# Tasks

## 1. Loading stages (builder)
- [ ] 1.1 play.py stage emission + launcher progress window — verify: `play.stage_lines`, `play.world_progress`, `app.loading_stages`, `app.loading_stages_warm`, `app.startup_failure` (extended)

## 2. One Dock icon (builder)
- [ ] 2.1 Shim (dylib) + wine_env wiring, scoped to app runs — verify: `app.shim_scope`, `play.dock_identity`
- [ ] 2.2 Launcher hand-off + focus forwarding + unminimise — verify: `app.single_dock_icon`, `app.handoff_focus`, `app.reopen_focuses_game`, `app.reopen_unminimizes`
- [ ] 2.3 Performance with vs without the shim — verify: `app.shim_perf`

## 3. Verify + human
- [ ] 3.1 Verifier pass + full harness
- [ ] 3.2 Human: Zach plays from the app (one icon, Cmd-Tab, full screen, Dock Quit, loading screen look)
