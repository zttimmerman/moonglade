# Spec Delta: mac-app

## ADDED Requirements

### Requirement: Reopen always brings Hearthstone forward
Opening or clicking Hearthstone SHALL bring forward whatever Hearthstone is showing: the loading window while loading, the game after.

#### Scenario: Click during loading
- **GIVEN** the app opened and its loading window up, with another app frontmost
- **WHEN** the app is reopened through LaunchServices before the game window appears
- **THEN** within 2 s the loading window is frontmost, no "already running" dialog is shown, and loading continues to the game
- Check: `app.reopen_during_loading`

### Requirement: Shutdown is visible
After the game exits or the player quits from the Dock, the launcher SHALL show a shutdown window until everything has stopped, then exit.

#### Scenario: Quit shows the shutdown window until the stack is down
- **GIVEN** the game running from the app
- **WHEN** the launcher is asked to quit (or the game exits)
- **THEN** a shutdown window ("Saving and shutting down the world…", advising not to force-quit) is shown from the start of shutdown until the stack is down, its stages are recorded in order, the app exits 0 after the stack is down, and nothing remains running
- Check: `app.shutdown_window`

#### Scenario: Zach sees the shutdown window
- **WHEN** Zach quits from the Dock
- **THEN** the shutdown window explains what's happening until the app closes
- [human-verify] Look and clarity are judged by a person.
