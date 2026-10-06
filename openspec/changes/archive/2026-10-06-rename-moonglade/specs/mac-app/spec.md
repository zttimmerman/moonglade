# Spec Delta: mac-app

## ADDED Requirements

### Requirement: The app is named Moonglade
The app SHALL be `Moonglade.app` with bundle identifier `local.moonglade.app`, and every user-visible name (bundle name, Dock and Cmd-Tab name, launcher windows and dialogs, game-process display name) SHALL be "Moonglade".

#### Scenario: Built bundle carries the new name
- **GIVEN** a clean build via `harness/hs app`
- **WHEN** the bundle is inspected
- **THEN** `dist/Moonglade.app` exists with CFBundleName/CFBundleDisplayName "Moonglade", bundle id `local.moonglade.app` and designated requirement `identifier "local.moonglade.app"`, and no old project name appears in the launcher executable, Info.plist or shim
- Check: `app.bundle_valid`

#### Scenario: Running app shows the new name
- **GIVEN** a built app
- **WHEN** it is opened with the stand-in client
- **THEN** the launcher's and the game process's LaunchServices display names are "Moonglade", and no user-visible launcher text says "Hearthstone"
- Check: `app.names_running`
