# Design: Save backups

## Decisions (Zach, 2026-10-06)
- **Whole save**, not per-account extraction: a backup is `acore_characters` + `acore_auth` + `acore_playerbots` (+ the
  player's `WTF/Account/<ACCOUNT>/`). AzerothCore's schema has dozens of per-character tables (items, mail, pets, auras,
  achievements, …); extracting one account's rows means a hand-kept table list where a miss restores a broken character.
  A whole-database dump is consistent by construction. Cost: restoring rewinds companions and AI players too (one
  profile today, so nothing else is affected). Revisit if multiple profiles become real.
- **Keep 10 automatic, 3 safety.**
- **UI option A:** automatic on quit; Option-click opens Backups.

## Mechanism
- **New module `harness/hslib/backups.py`**: `take(kind, account, client_dir, dest_root) -> manifest`, `list(dest_root)`,
  `verify(backup)`, `restore(backup_id, ...)`, `rotate(dest_root)`. CLI: `./play --backups [list|restore <id>] [--json]`
  (the app's Backups window calls the JSON form).
- **Dump:** `mysqldump --single-transaction --routines --triggers --add-drop-database --databases <db>` per database,
  through the isolated server's socket/port (127.0.0.1:3307, never the Homebrew MySQL), compressed (gzip or zstd,
  builder's choice, recorded here). Each file's sha256 goes in `manifest.json`.
- **When:** `stack.down` gets an optional hook that runs after authserver stops and before mysqld stops. The play loop
  passes it only for a genuine player launch (the same gate as `resolution_gate(...)["apply"]`), or for a test run that
  names a scratch folder through `HS_BACKUP_DIR` plus the explicit test flag. A new shutdown stage `shutdown_backup`
  ("Backing up your save") sits between `shutdown_login` and `shutdown_database`; when there's no backup it is `skipped`.
- **Atomic and kill-safe:** write into `<root>/.tmp-<id>/`, fsync, verify the checksums, then `rename` to `<root>/<id>/`.
  Rotation runs only after the rename. Every entry point removes stale `.tmp-*` folders first. Only folders with a valid
  `manifest.json` are listed.
- **Free-space guard:** skip with a recorded failure when free disk < 2 GB.
- **Failure record:** `env/run/play-last.json` gets a `backup` block (ok, id or error); the next launch prints a
  `[notice]` line ("last backup failed: …") that the app shows.
- **Restore:** refuse if the play lock is held; verify the checksums (refuse on mismatch, change nothing); start mysqld
  only (no auth/world server); take a `before-restore` safety backup; load each dump; replace the WTF account folder from
  the backup; stop mysqld. Older backups load fine: AzerothCore's `updates` tables are inside the dumps, so the
  worldserver's DB updater applies any newer SQL updates on the next start.
- **Where:** `env/backups/` (gitignored). Folder names `YYYYMMDD-HHMMSS-<kind>`.
- **App:** `launcher.m` checks `[NSEvent modifierFlags] & NSEventModifierFlagOption` at launch (test-injectable only via
  the existing test hook, e.g. `HS_APP_TEST_OPTION=1`), shows a Backups window (table + Restore/Play/Quit), confirmation
  via `NSAlert`, and runs `./play --backups … --json` for data and actions.

## Measurements
(builder fills in: backup size and duration with the default world, restore duration)
