# harness-reporting Specification

## Purpose
One headless command runs every check and produces a machine-readable result and a human summary, so verifiers and future sessions can judge state without reading logs.

## Requirements

### Requirement: Single-command full run
`harness/hs run` SHALL run every registered check in the selected tier (default `full`), optionally filtered by `--only <glob>`, bring the stack up as needed, and exit 0 only if every executed check passed. The one exception: on a run that isn't full (filtered, or tier `quick`), `report.full_run` records `skip` ("not a full run") instead of `fail`, and that skip doesn't affect the exit code. On a full run, `report.full_run` must pass. A run counts as **full** only if it is unfiltered and its tier is `full` or `slow`, and it SHALL include every check referenced by any scenario in the archived specs and in the active changes (those listed in `harness/active-changes`).

#### Scenario: Full run produces reports
- **GIVEN** a built server
- **WHEN** `harness/hs run` runs
- **THEN** `harness/results.json` and `harness/summary.md` are written, and the exit code is 0 iff every check's status is `pass` (`report.full_run` may only be `skip` on a non-full run, and must be `pass` on a full run)
- Check: `report.full_run`

#### Scenario: Every spec scenario maps to a check
- **GIVEN** the specs under `openspec/specs/` and the spec deltas of every change listed in `harness/active-changes`
- **WHEN** the harness runs its coverage self-check
- **THEN** every scenario has either a `Check: <id>` naming a registered check, or a `[human-verify]` tag with a reason; any other scenario makes this check fail and is listed
- Check: `report.coverage`

### Requirement: Results schema
`results.json` SHALL contain: run metadata (start/end time, git SHA of this repo, `fork.lock` SHAs, host info) and, per check, `id`, `status` (`pass`|`fail`|`error`|`skip`), `duration_s`, and `evidence` (commands run, key output excerpts, expected vs. actual).

#### Scenario: Schema is valid
- **GIVEN** a completed run
- **WHEN** `results.json` is validated against `harness/results.schema.json`
- **THEN** it validates, and every check record has non-empty evidence
- Check: `report.schema`

### Requirement: Test tiers
Every check SHALL declare a tier: `quick`, `full`, or `slow`. `harness/hs run --tier quick` SHALL run only `quick` checks. `--tier full` (the default) SHALL run `quick` and `full` checks. `--tier slow` SHALL run all checks. `results.json` SHALL record the tier.

#### Scenario: Quick tier runs fast and says so
- **GIVEN** a built server
- **WHEN** `harness/hs run --tier quick` runs
- **THEN** it executes every `quick` check and no `full`/`slow` check, finishes in under 3 minutes on the target Mac, and records `tier: quick` in results.json. `report.full_run` reports `skip` ("not a full run"), and the command exits 0 iff every other executed check passed
- Check: `report.tiers`

#### Scenario: Every check has a tier
- **WHEN** the harness lists registered checks
- **THEN** each has exactly one tier from {quick, full, slow}, and `build.clean`, `client.launch_smoke` and `lifecycle.persist` are not `quick`
- Check: `report.tiers_declared`

### Requirement: Slow-tier scenario checks
A check that a spec scenario references MAY be tagged `slow` when it needs long warm-ups or soak sampling. A **full**-tier run SHALL record such checks as `skip` ("slow tier, not run in a full run"), and these skips SHALL NOT fail `report.full_run` or the exit code. A **slow**-tier run SHALL execute and require them like any other check. A change whose spec deltas reference a slow-tier check SHALL be verified with at least one unfiltered `--tier slow` run before it merges.

#### Scenario: Full run skips slow scenario checks; slow run requires them
- **GIVEN** a registered check tagged `slow` and referenced by an active change's scenario
- **WHEN** an unfiltered full-tier run executes, and then an unfiltered slow-tier run executes
- **THEN** the full run records that check as `skip` with "slow tier", `report.full_run` passes, and the exit code ignores that skip; the slow run executes it, and `report.full_run` passes there only if it passed
- **AND** a skip of a non-slow check in a full run still fails the run
- Check: `report.slow_tier_rule`

### Requirement: GUI tier
Checks that need exclusive screen focus SHALL be marked `gui`. A full run SHALL skip them by design unless the change touches GUI-related paths, in which case it SHALL run them.

#### Scenario: Full run skips GUI checks when nothing GUI-related changed, and requires them otherwise
- **GIVEN** the harness with GUI-marked checks
- **WHEN** a full run happens (a) with no change to the GUI path list and (b) with a change to one of those paths
- **THEN** in (a) every GUI check is recorded as skipped with a by-design reason, and `report.full_run` passes; in (b) every GUI check runs, and a failing or skipped GUI check fails `report.full_run`; `--tier gui` runs exactly the GUI checks
- Check: `report.gui_tier_rule`

### Requirement: Full-run timing is recorded and guarded
The harness SHALL record per-check timing for every run. An unfiltered full run SHALL fail the timing guard only for a real slowdown: the checks recorded in the per-check timing baseline, taken together, SHALL NOT take more than 10% longer than their baseline times on the same machine, with no baseline check removed or weakened. Time spent building server binaries (`build.clean`, and any baseline-server rebuild inside a check) SHALL be reported separately and excluded from that comparison. Checks not in the baseline SHALL be reported with their times and SHALL NOT fail the guard; any single such check taking more than 180 s SHALL be listed as a warning in the summary. A check joins the baseline, with its time from the merging change's verifier full run, when that change merges; existing baseline times are never raised that way.

#### Scenario: Timing summary and no regression
- **GIVEN** a per-check timing baseline recorded in the repo (each check's time, with the run it came from)
- **WHEN** an unfiltered full run completes
- **THEN** results.json has a timing summary (per check, total, top 10, build time, backups-tier total, new checks and their times, warnings); every baseline check that the run's tiers execute ran (minus by-design GUI and backups-tier skips); and the sum of the executed baseline checks' times, minus build time, is ≤110% of the same checks' baseline sum
- Check: `report.timing`

#### Scenario: Slowdowns fail, new checks and rebuilds don't
- **GIVEN** synthetic timing records against a known baseline
- **WHEN** (a) the baseline checks are 15% slower in total, (b) they're unchanged but 300 s of new checks are added, (c) they're unchanged but a from-empty build adds 400 s, (d) a single new check takes 200 s
- **THEN** (a) fails; (b) and (c) pass with the new time and build time reported; (d) passes with a warning naming the check
- Check: `report.timing`

### Requirement: Leftover-process detection only counts our processes
Leftover-process detection SHALL count only this user's Wine/Wow processes started from the pinned runtime, the client or the play client, never unrelated processes that merely hold a file open under the prefix.

#### Scenario: A system process with an open file under the prefix isn't counted
- **GIVEN** an unrelated process (a stand-in for macOS's StorageManagementService) holding a file open under `env/wineprefix-mtld3d`
- **WHEN** `prefix_processes()` runs
- **THEN** that process isn't reported, while a real Wine process from the runtime is
- Check: `harness.prefix_scope`

### Requirement: GUI path rule covers shared helpers
The GUI path rule SHALL include the shared harness modules the GUI checks depend on, so changing one of them runs the GUI checks in a full run.

#### Scenario: Changing a shared helper triggers the GUI checks
- **GIVEN** a change that touches only a shared helper used by the GUI session (e.g. `harness/hslib/procs.py`)
- **WHEN** the GUI path rule is evaluated
- **THEN** it reports a GUI path change, and a full run would run the GUI checks
- Check: `report.gui_tier_rule`

### Requirement: Old project name doesn't come back
Tracked current files SHALL NOT use the old project name "Hearthstone", except in recorded history and in Blizzard's own game content.

#### Scenario: Naming guard
- **GIVEN** the repository at HEAD
- **WHEN** the naming guard scans tracked files case-insensitively for "hearthstone"
- **THEN** every hit is inside the documented allowlist (archived changes, research docs, ADR bodies, Blizzard game data/strings, this change and the guard itself), and adding the word to any other tracked file makes the guard fail
- Check: `repo.naming`

### Requirement: Folder rename is safe
The folder-rename script SHALL default to a dry run and, with `--apply`, move the repo folder and Claude Code's per-project data together and repair git worktree paths.

#### Scenario: Dry run and apply in a scratch copy
- **GIVEN** a scratch copy of a repo with a worktree, and a scratch Claude project-data folder named after its path
- **WHEN** the script runs without and then with `--apply` against the scratch paths
- **THEN** the dry run changes nothing and prints the plan; the apply moves the folder and the project-data folder to the new encoded name, `git worktree list` shows valid paths, and nothing outside the scratch paths is touched
- Check: `repo.rename_script`

### Requirement: Backups tier
Checks that exercise save backups end to end (every `backups.*` check and `app.backups_window`) SHALL be marked `backups`. A full run SHALL skip them by design unless the change touches backup-related paths, in which case it SHALL run them and require them like any other check. A slow-tier run (the periodic "run everything" sweep) SHALL always run them. The backup path list SHALL include at least `harness/hslib/backups.py`, `harness/hslib/play.py`, `harness/hslib/stack.py`, `harness/hslib/stages.py`, `harness/app/launcher.m`, `play` and the backup checks' own source file.

#### Scenario: Full run skips backup checks when no backup path changed, and requires them otherwise
- **GIVEN** the harness with backups-marked checks
- **WHEN** a full run happens (a) with no change to the backup path list and (b) with a change to one of those paths; and a slow-tier run happens
- **THEN** in (a) every backups-marked check is recorded as skipped with a by-design reason, and `report.full_run` passes; in (b) every backups-marked check runs, and a failing or skipped one fails `report.full_run`; the slow run executes them all; and `--tier backups` runs exactly the backups-marked checks
- Check: `report.backups_tier_rule`
