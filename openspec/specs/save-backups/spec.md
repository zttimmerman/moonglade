# save-backups Specification

## Purpose
TBD - created by archiving change save-backups. Update Purpose after archive.

## Requirements

### Requirement: Automatic backup on quit
When a genuine player session ends, the play loop SHALL take a backup after the world server has stopped and before the database stops. A backup is a consistent snapshot of the `acore_characters`, `acore_auth` and `acore_playerbots` databases plus the play client's `WTF/Account/<ACCOUNT>/` folder for the player's account, written atomically with a manifest (creation time, kind, core and playerbots pins, the player's characters with name, level and class, and a sha256 per file).

#### Scenario: A session ends and a backup appears
- **GIVEN** a backup folder with no backups and a play session run with a test-account character that gained a level during the session (scratch backup folder and scratch play client via the test switches)
- **WHEN** the client exits and the play loop shuts down
- **THEN** exactly one new backup exists; its manifest lists the three databases and the WTF folder with sha256 values that match the files; the manifest's character list includes that character at its new level; the backup's start time is after the worldserver process exited and its end time is before mysqld stopped; and the stack is fully down afterwards
- Check: `backups.on_quit`

#### Scenario: A backup restores to exactly what was saved
- **GIVEN** a backup taken on quit
- **WHEN** the backup is loaded into a scratch MySQL schema set and compared with the data at backup time
- **THEN** every table in the three databases has the same row count and the same checksum (`CHECKSUM TABLE`) as at backup time
- Check: `backups.on_quit`

#### Scenario: Harness and test runs never touch the player's backups
- **GIVEN** the player's backup folder `env/backups/` with its current contents fingerprinted
- **WHEN** a full harness run completes (including every play-loop and app check)
- **THEN** `env/backups/` is byte-identical to before; a test-shaped run (test account, stand-in client or any harness sign) that does not name a scratch backup folder takes no backup; and naming a scratch folder (`HS_BACKUP_DIR`) without the explicit test flag is refused with a nonzero exit before the server starts
- Check: `backups.player_untouched`

### Requirement: Keep the last 10
Automatic backups beyond the newest 10 SHALL be deleted after each new backup is complete. "Before restore" safety backups SHALL be kept separately, the newest 3.

#### Scenario: Rotation keeps the newest
- **GIVEN** a scratch backup folder holding 10 automatic and 3 safety backups
- **WHEN** two more automatic backups and one more safety backup are taken
- **THEN** exactly the newest 10 automatic and newest 3 safety backups remain, and no backup is deleted before the new one is complete
- Check: `backups.rotation`

### Requirement: A failed backup never blocks shutdown
If a backup can't be taken (an error, or less than 2 GB of free disk), shutdown SHALL continue and stop everything, nothing partial SHALL be listed as a backup, and the next open SHALL tell the player the last backup failed and why.

#### Scenario: Backup fails, shutdown still completes
- **GIVEN** a play session where the backup is made to fail (an injected dump failure, then an injected low-disk reading)
- **WHEN** the session ends
- **THEN** in both cases the stack is fully down, no new backup is listed, no temporary backup files remain, the failure is recorded in `env/run/play-last.json`, and the next launch's output carries a "last backup failed" notice naming the reason
- Check: `backups.failure_nonblocking`

#### Scenario: Killed in the middle of a backup
- **GIVEN** a play session whose backup is in progress
- **WHEN** the play loop is killed with SIGKILL during the dump
- **THEN** no partial backup is listed, earlier backups are intact (checksums unchanged), and the next run removes the leftover temporary files and works normally
- Check: `backups.kill_safe`

### Requirement: Restore with a safety backup
Restoring a backup SHALL first take a "before restore" safety backup, then replace the three databases and the player's WTF account folder with the backup's contents, with the world server stopped. A backup whose files don't match its manifest checksums SHALL be refused without changing anything. Restore SHALL be refused while Moonglade (the app or `./play`) is running. A restore that is interrupted SHALL be finished by the next `./play` or restore before anything else runs. **Tests never restore into the live databases:** a test-shaped restore (scratch backup folder via the test flag) SHALL target scratch schemas only (names starting `hsbk_`), and a restore into the live `acore_*` schemas from a test-shaped run SHALL be refused before anything is changed.

#### Scenario: Restore rewinds, and can be undone
- **GIVEN** a backup B, after which the test character's level is changed and a new test character is created
- **WHEN** B is restored (`./play --backups restore <id>` with the scratch test switches, into scratch `hsbk_` schemas)
- **THEN** a safety backup S is taken first; afterwards the three target schemas match B's checksums (the level is back and the new character is gone) and the WTF account folder matches B; the stack is down afterwards; and restoring S brings back the changed level and the new character
- Check: `backups.restore_roundtrip`

#### Scenario: Damaged backup is refused
- **GIVEN** a backup with one byte of one dump file changed
- **WHEN** it is restored
- **THEN** the restore is refused with a message naming the damaged file, exits nonzero, and every table checksum is unchanged; no safety backup is taken
- Check: `backups.restore_refusals`

#### Scenario: A test run can't restore into the live databases
- **GIVEN** the live `acore_characters`, `acore_auth` and `acore_playerbots` with every table's checksum recorded
- **WHEN** a test-shaped restore names the live schemas as its target (and, separately, names no target)
- **THEN** both are refused with a nonzero exit before any database is touched, no safety backup is taken, and every live table checksum is unchanged
- Check: `backups.restore_refusals`

#### Scenario: Killed in the middle of a restore
- **GIVEN** a test-shaped restore into scratch `hsbk_` schemas, in progress (loading)
- **WHEN** it is killed with SIGKILL, and then `./play` (test-shaped, scratch folder) runs
- **THEN** before starting anything else the next run finishes the restore (the scratch schemas then match the backup's checksums), no loader process is left, the backup and its safety backup are intact, and every live table checksum is unchanged throughout
- Check: `backups.kill_safe`

#### Scenario: Restore while running is refused
- **GIVEN** a running play session (the play lock held)
- **WHEN** a restore is requested
- **THEN** it is refused with an "already running" message, exits nonzero, and the session continues unaffected
- Check: `backups.restore_refusals`

#### Scenario: Zach restores a backup
- **GIVEN** a few sessions of real play
- **WHEN** Zach Option-clicks Moonglade, picks an older backup and restores it, then plays
- **THEN** the game shows the characters as they were at that backup
- [human-verify] Whether the rewind matches what Zach remembers can only be judged by the player.
