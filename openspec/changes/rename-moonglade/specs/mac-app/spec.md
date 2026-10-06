# Spec Delta: mac-app

## ADDED Requirements

### Requirement: The app is named Moonglade
The app SHALL be `Moonglade.app` with bundle identifier `local.moonglade.app`, and every user-visible name (bundle name, Dock and Cmd-Tab name, launcher windows and dialogs, game-process display name) SHALL be "Moonglade".

#### Scenario: Built bundle and running app carry the new name
- **GIVEN** a clean build via `harness/hs app`
- **WHEN** the bundle is inspected and the app is opened with the stand-in client
- **THEN** `dist/Moonglade.app` exists with CFBundleName/CFBundleDisplayName "Moonglade", bundle id `local.moonglade.app` and designated requirement `identifier "local.moonglade.app"`; the launcher's and the game process's LaunchServices display names are "Moonglade"; no user-visible launcher text says "Hearthstone"
- Check: `app.bundle_valid`
