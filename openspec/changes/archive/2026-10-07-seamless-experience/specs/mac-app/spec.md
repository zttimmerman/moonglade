# Spec Delta: mac-app

## ADDED Requirements

### Requirement: Friendly launcher errors
The launcher's error dialogs SHALL state the problem in plain words, with no server/database/port wording in the main text, and offer **Show details** that reveals the technical error and the log path.

#### Scenario: Startup failure is friendly, with details
- **GIVEN** a built app and a stack that can't reach readiness (the existing startup-failure setup)
- **WHEN** the app is opened
- **THEN** the dialog's main text has none of the words server, database, MySQL, port, realm or SOAP; the details text names the technical error and the log path; and the app exits nonzero with nothing running (as before)
- Check: `app.startup_failure`

### Requirement: Restore confirms when it's done
After a restore from the Backups window, the window SHALL say that the restore finished and which save is now in place ("Restored. Your save is now from <date>."), or that it failed and why.

#### Scenario: Restore shows a done message
- **GIVEN** the Backups window with a scratch backup folder (test hook)
- **WHEN** a restore is confirmed and completes, and separately a restore fails (damaged backup)
- **THEN** the window shows the done message naming the backup's date after success, and a failure message naming the reason after failure, before Play or Quit is chosen
- Check: `app.backups_window`
