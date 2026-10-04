# Tasks

## 1. Icon (builder)
- [ ] 1.1 Icon generator: circular transparent PNG + `AppIcon.icns` from the logo source; commit the outputs — verify: `app.icon`

## 2. App (builder)
- [ ] 2.1 `hs app` builds `dist/Hearthstone.app` (plist, launcher, icon, repo path, ad-hoc signature) — verify: `app.bundle_valid`
- [ ] 2.2 Launcher runs the play loop headlessly with status/error dialogs and the single-instance message — verify: `app.launch_lifecycle`, `app.startup_failure`, `app.single_instance`, `app.relocatable`

## 3. Verify + human
- [ ] 3.1 Verifier pass + full harness
- [ ] 3.2 Human: Zach double-clicks the app from Finder/Dock
