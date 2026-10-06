# Proposal: Save backups (auto on quit, restore from an Option-click Backups window)

## Why
Zach is about to play seriously (M2/M3 human-verify, then M4 dungeons). Today a bad harness run, a bug in a future change,
or a mistake (deleting a character, a botched vanilla-mode toggle) can lose characters with no way back. A save backup
taken on every quit makes progress safe, the way a console game keeps save slots.

## What Changes
- **Automatic backup on quit.** When a genuine player session ends (the app or `./play` with no test arguments), the play
  loop takes a backup after the world server has stopped (so every character is saved) and before the database stops.
  The shutdown window shows a new stage, "Backing up your save".
- **What a backup is (Zach, 2026-10-06: "whole save").** A consistent snapshot of the `acore_characters`, `acore_auth`
  and `acore_playerbots` databases, plus the player's client per-account UI settings (`WTF/Account/<ACCOUNT>/` in the play
  client: keybindings, macros, chat and addon settings). Restoring rewinds the player's characters, companions and the AI
  players' records together. The world database (game content, vanilla-mode data) is never part of a backup.
- **Keep the last 10 (Zach, 2026-10-06).** Older automatic backups are deleted. A restore first saves a "before restore"
  safety backup (the last 3 are kept, counted separately), so any restore can be undone.
- **Restore.** Holding Option while opening Moonglade shows a Backups window instead of starting the game: a list of
  backups (date and time, characters with level and class), Restore (with a confirmation that says what will be rewound
  and that a safety backup is taken first), then Play or Quit. `./play --backups` gives the same from a terminal.
- **Safety.** A failed backup never blocks shutdown (the player is told on the next open). Backups are written atomically
  and carry checksums; a damaged backup is refused, not restored. Restore is refused while Moonglade is running. Harness
  runs never read or write the player's backups (scratch folder only, via a refused-by-default test switch).
- Backups live in `env/backups/` (gitignored, inside the repo). Portable packages (M5 item 8) will move saves to the OS
  app-data folder later.

## Capabilities
### New Capabilities
- `save-backups`: automatic backups on quit, rotation, verified restore, safety rules.

### Modified Capabilities
- `mac-app`: Option-click opens the Backups window; the shutdown window shows the backup stage.
- `harness-reporting`: a path-triggered backups tier; the full-run timing guard excludes it.

## Data provenance (§7)
No game data values are written. Backups are byte-for-byte dumps of the player's own databases.

## Open points
- Size and time were estimated, not measured: `acore_characters` is ~413 MB on disk with the ~500-bot world, so a
  compressed dump is expected around 30–60 MB and 15–30 s. The builder measures both and records them in design.md;
  if a backup takes over 60 s, raise it at the next human gate.

## Spec-change log
- 2026-10-06 (tightening, after an incident): B25's manual kill-during-restore test ran against the live databases and its cleanup deleted the backups being restored, half-emptying `acore_characters` (recovered from a full copy with Zach's OK; all 111 tables checksum-identical to the 08:47 copy, nothing of the player's lost). The spec allowed tests to restore into the live schemas. Now: test-shaped restores target scratch `hsbk_` schemas only and live targets are refused (new scenario), and a killed restore is healed by the next run (new scenario under `backups.kill_safe`).
- 2026-10-06 (Zach's choice; **loosening, review at the M4 gate**): the verifier's full run took 2433 s against the 1868 s timing limit; the new backup checks cost ~690 s. Zach chose "run them only when needed": a backups tier that full runs execute only when backup-related paths change (like the GUI tier), plus the slow-tier sweep, and `report.timing` excludes backups-tier time from the baseline comparison. Rejected: accepting ~40 min full runs, or slimming the checks first (likely still over the limit, +3 h).
