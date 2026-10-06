# Spec Delta: harness-reporting

## ADDED Requirements

### Requirement: Backups tier
Checks that exercise save backups end to end (every `backups.*` check and `app.backups_window`) SHALL be marked `backups`. A full run SHALL skip them by design unless the change touches backup-related paths, in which case it SHALL run them and require them like any other check. A slow-tier run (the periodic "run everything" sweep) SHALL always run them. The backup path list SHALL include at least `harness/hslib/backups.py`, `harness/hslib/play.py`, `harness/hslib/stack.py`, `harness/hslib/stages.py`, `harness/app/launcher.m`, `play` and the backup checks' own source file.

#### Scenario: Full run skips backup checks when no backup path changed, and requires them otherwise
- **GIVEN** the harness with backups-marked checks
- **WHEN** a full run happens (a) with no change to the backup path list and (b) with a change to one of those paths; and a slow-tier run happens
- **THEN** in (a) every backups-marked check is recorded as skipped with a by-design reason, and `report.full_run` passes; in (b) every backups-marked check runs, and a failing or skipped one fails `report.full_run`; the slow run executes them all; and `--tier backups` runs exactly the backups-marked checks
- Check: `report.backups_tier_rule`

## MODIFIED Requirements

### Requirement: Full-run timing is recorded and guarded
The harness SHALL record per-check timing for every run, and an unfiltered full run SHALL NOT take more than 10% longer than the recorded baseline on the same machine, with no check removed or weakened. Time spent in backups-tier checks (which the baseline doesn't contain) SHALL be reported separately and excluded from the wall time compared with the baseline.

#### Scenario: Timing summary and no regression
- **GIVEN** the baseline full run (86 pass, ~1690 s at 9265c01) recorded in the repo
- **WHEN** an unfiltered full run completes after this change
- **THEN** results.json has a timing summary (per check, total, top 10, and the backups-tier total), the run has at least as many executed checks with the same ids (minus by-design GUI and backups-tier skips) as the baseline, and its wall time minus the backups-tier checks' time is ≤110% of the baseline
- Check: `report.timing`
