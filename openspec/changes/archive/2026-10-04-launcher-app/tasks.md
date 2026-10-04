# Tasks

## 1. Icon (builder)
- [x] 1.1 Icon generator: circular transparent PNG + `AppIcon.icns` from the logo source; commit the outputs — verify: `app.icon`

## 2. App (builder)
- [x] 2.1 `hs app` builds `dist/Hearthstone.app` (plist, launcher, icon, repo path, ad-hoc signature) — verify: `app.bundle_valid`
- [x] 2.2 Launcher runs the play loop headlessly with status/error dialogs and the single-instance message — verify: `app.launch_lifecycle`, `app.startup_failure`, `app.single_instance`, `app.relocatable`

## 3. Verify + human
- [x] 3.1 Verifier pass + full harness (PASS at 1fea887: full 63/0, app.* 7/7 each, counterexamples per check)
- [ ] 3.2 (pending: Zach double-clicks after merge) Human: Zach double-clicks the app from Finder/Dock
