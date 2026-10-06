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
Measured 2026-10-06 on the dev Mac with the ~500-bot world's data (acore_characters 414 MB on disk, playerbots 103 MB):
- **Backup:** 37.6 MB per backup (gzip -6), **9.5-9.7 s** (characters dump 2.2-2.3 s, auth 0.15 s, playerbots 5.5 s,
  plus CHECKSUM TABLE stats and hashing). Well under the 60 s gate threshold.
- **Restore:** **~29 s** end to end (mysqld start 1.1 s, safety backup ~9.5 s, load 17-18 s: characters ~9 s,
  playerbots ~7 s, auth 0.1 s; then every table re-checksummed against the manifest).

## As built
- `harness/hslib/backups.py`: plan (the gate), take/list/verify/rotate, on_quit, restore (+ rollback to the safety
  backup if loading or the post-load table comparison fails), finish_interrupted, cli (`./play --backups`).
- **Dump format differs from the design's `mysqldump --databases`:** mysqldump prints FLOAT columns with 6 significant
  digits, so a reload changed CHECKSUM TABLE for characters, character_homebind and corpse (measured). Each database
  is one gzip stream of: mysqldump schema (`--no-data --routines --events --skip-triggers`), mysqldump data minus
  tables with FLOAT/DOUBLE columns, our own exact INSERTs for those tables (`CAST(col AS DOUBLE)`, strings as hex
  with charset introducers), then triggers. No `--databases`: the restore drops/creates each database with the
  charset/collation recorded in the manifest and loads into it, which also lets a check load a dump into scratch
  schemas. Compression: gzip -6 (/usr/bin/gzip; zstd was no smaller here).
- The manifest also records per-table row counts and CHECKSUM TABLE EXTENDED values; a restore compares every table
  with them after loading.
- take() refuses unless world and login servers are stopped and mysqld runs (consistency without locks).
- `stack.down(before_database=...)` runs the hook only when world+login are gone and mysqld runs; the stage is
  `shutdown_backup` (start/done, `failed` + error, or `skipped`). Backups happen only when the client started.
- Failure record: play-last.json `backup`, plus `<root>/.last-backup.json` (so a harness run between two player
  sessions can't eat the notice); the next launch prints `[notice] Last backup failed (...): <reason>...`.
- Test switch: `$HS_BACKUP_DIR` reuses the existing explicit test flag `--allow-test-client-dir`; it may not point at,
  inside or above env/backups/. Knobs `HS_BACKUP_TEST_FAIL=dump`, `HS_BACKUP_TEST_FREE_BYTES`, `HS_BACKUP_TEST_PAUSE_S`
  only with a scratch folder. `./play --backups` in a harness-shaped environment without a scratch folder is refused.
- Kill-safety: `.tmp-<id>/` records its writer and child pids; every entry point removes stale temp folders (killing
  a dead writer's leftover dump processes). A restore writes `<root>/.restore-in-progress.json` before loading; the
  next ./play or restore finishes it (kills orphaned loaders, reloads the target, re-checks every table) before
  anything else (added after the first verification round of the builder; see the builder report).
- App: Option at launch (`[NSEvent modifierFlags]`, or `HS_APP_TEST_OPTION=1` with the test hook) shows the Backups
  window (NSTableView, Restore…/Quit/Play, NSAlert confirmation; scripted with `HS_APP_TEST_BACKUPS` under the hook);
  `[notice]` lines show in an orange line of the loading window. The shutdown window needed no change.
- app.shutdown_window: the existing shared-session assertions are unchanged (the session takes no backup, so its
  stage list now carries `shutdown_backup skipped`); the new scenario uses a second app run where the game exits by
  itself with a scratch backup folder.
