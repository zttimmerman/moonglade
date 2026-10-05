# Spec Delta: harness-reporting

## ADDED Requirements

### Requirement: GUI tier
Checks that need exclusive screen focus SHALL be marked `gui`. A full run SHALL skip them by design unless the change touches GUI-related paths, in which case it SHALL run them.

#### Scenario: Full run skips GUI checks when nothing GUI-related changed, and requires them otherwise
- **GIVEN** the harness with GUI-marked checks
- **WHEN** a full run happens (a) with no change to the GUI path list and (b) with a change to one of those paths
- **THEN** in (a) every GUI check is recorded as skipped with a by-design reason, and `report.full_run` passes; in (b) every GUI check runs, and a failing or skipped GUI check fails `report.full_run`; `--tier gui` runs exactly the GUI checks
- Check: `report.gui_tier_rule`

### Requirement: Full-run timing is recorded and faster
The harness SHALL record per-check timing for every run, and an unfiltered full run SHALL take at least 30% less wall time than the recorded baseline on the same machine, with no check removed or weakened.

#### Scenario: Timing summary and speed-up
- **GIVEN** the baseline full run (86 pass, ~1690 s at 9265c01) recorded in the repo
- **WHEN** an unfiltered full run completes after this change
- **THEN** results.json has a timing summary (per check, total, top 10), the run has at least as many executed checks with the same ids (minus by-design GUI skips) as the baseline, and its wall time is ≤70% of the baseline
- Check: `report.timing`

### Requirement: Leftover-process detection only counts our processes
Leftover-process detection SHALL count only this user's Wine/Wow processes started from the pinned runtime, the client or the play client, never unrelated processes that merely hold a file open under the prefix.

#### Scenario: A system process with an open file under the prefix isn't counted
- **GIVEN** an unrelated process (a stand-in for macOS's StorageManagementService) holding a file open under `env/wineprefix-mtld3d`
- **WHEN** `prefix_processes()` runs
- **THEN** that process isn't reported, while a real Wine process from the runtime is
- Check: `harness.prefix_scope`
