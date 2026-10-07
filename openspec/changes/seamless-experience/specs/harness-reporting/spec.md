# Spec Delta: harness-reporting

## MODIFIED Requirements

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
