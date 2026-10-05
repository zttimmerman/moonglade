# Tasks

## 1. Loading stages (builder)
- [x] 1.1 play.py stage emission + launcher progress window — verify: `play.stage_lines`, `play.world_progress`, `app.loading_stages`, `app.loading_stages_warm`, `app.startup_failure` (extended)

## 2. One Dock icon (builder)
- [x] 2.1 Shim (dylib) + wine_env wiring, scoped to app runs — verify: `app.shim_scope`, `play.dock_identity`
- [x] 2.2 Launcher hand-off + focus forwarding + unminimise — verify: `app.single_dock_icon`, `app.handoff_focus`, `app.reopen_focuses_game`, `app.reopen_unminimizes`
- [x] 2.3 Performance with vs without the shim — verify: `app.shim_perf`

## 3. Verify + human
- [x] 3.1 Verifier pass + full harness (cycle 2 PASS at 9265c01: full 86/0, GUI checks 4/4, shim_perf 2/2 + GIVEN counterexamples; cycle 1 FAIL = shim_perf env flake, fixed)
- [ ] 3.2 (pending: Zach plays from the app) Human: Zach plays from the app (one icon, Cmd-Tab, full screen, Dock Quit, loading screen look)
