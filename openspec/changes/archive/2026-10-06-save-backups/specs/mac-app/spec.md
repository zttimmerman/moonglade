# Spec Delta: mac-app

## ADDED Requirements

### Requirement: Option-click opens Backups
Opening Moonglade with the Option key held SHALL show a Backups window instead of starting the game. It lists every backup, newest first, with date and time, kind (automatic or before restore) and the player's characters (name, level, class); offers Restore with a confirmation dialog that says what will be rewound and that a safety backup is taken first; and afterwards offers Play or Quit. A notice that the last backup failed SHALL appear in this window and in the loading window.

#### Scenario: Option-open lists backups and starts nothing
- **GIVEN** a built app, a scratch backup folder with 3 backups, and the Option key reported held at launch (test-injected through the app's test hook)
- **WHEN** the app is opened
- **THEN** the Backups window is shown listing the 3 backups newest first with the manifest's date, kind and characters; no stack process, Wine process or client is started; and closing the window exits the app 0 with nothing running
- Check: `app.backups_window`

#### Scenario: Restore from the window, then play
- **GIVEN** the Backups window from the previous scenario
- **WHEN** a backup is chosen and Restore is confirmed (test-injected), then Play is chosen
- **THEN** the confirmation text names the backup's date and says a safety backup is taken first; the restore runs (a safety backup appears and the databases match the chosen backup's checksums); and the normal loading flow then starts the stand-in client
- Check: `app.backups_window`

#### Scenario: Cancelling changes nothing
- **GIVEN** the Backups window
- **WHEN** Restore is chosen and the confirmation is cancelled (test-injected)
- **THEN** no safety backup is taken and every table checksum is unchanged
- Check: `app.backups_window`

#### Scenario: Zach opens Backups
- **WHEN** Zach holds Option and clicks Moonglade in the Dock or Finder
- **THEN** the Backups window is clear about what each backup is and what restoring does
- [human-verify] Clarity and the real Option-key path (not injected) are judged by a person.

## MODIFIED Requirements

### Requirement: Shutdown is visible
After the game exits or the player quits from the Dock, the launcher SHALL show a shutdown window until everything has stopped, then exit. On a genuine player session the stages SHALL include "Backing up your save" after the login server stops and before the database stops.

#### Scenario: Quit shows the shutdown window until the stack is down
- **GIVEN** the game running from the app
- **WHEN** the launcher is asked to quit (or the game exits)
- **THEN** a shutdown window ("Saving and shutting down the world…", advising not to force-quit) is shown from the start of shutdown until the stack is down, its stages are recorded in order, the app exits 0 after the stack is down, and nothing remains running
- Check: `app.shutdown_window`

#### Scenario: The backup stage is shown in order
- **GIVEN** the game running from the app with a scratch backup folder (test switches)
- **WHEN** the game exits
- **THEN** the recorded shutdown stages include `shutdown_backup` after `shutdown_login` and before `shutdown_database`, with the window visible during it, and a new backup exists in the scratch folder
- Check: `app.shutdown_window`

#### Scenario: Zach sees the shutdown window
- **WHEN** Zach quits from the Dock
- **THEN** the shutdown window explains what's happening until the app closes
- [human-verify] Look and clarity are judged by a person.
