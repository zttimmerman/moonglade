# Spec Delta: harness-reporting

## ADDED Requirements

### Requirement: Slow-tier scenario checks
A check that a spec scenario references MAY be tagged `slow` when it needs long warm-ups or soak sampling. A **full**-tier run SHALL record such checks as `skip` ("slow tier, not run in a full run"), and these skips SHALL NOT fail `report.full_run` or the exit code. A **slow**-tier run SHALL execute and require them like any other check. A change whose spec deltas reference a slow-tier check SHALL be verified with at least one unfiltered `--tier slow` run before it merges.

#### Scenario: Full run skips slow scenario checks; slow run requires them
- **GIVEN** a registered check tagged `slow` and referenced by an active change's scenario
- **WHEN** an unfiltered full-tier run executes, and then an unfiltered slow-tier run executes
- **THEN** the full run records that check as `skip` with "slow tier", `report.full_run` passes, and the exit code ignores that skip; the slow run executes it, and `report.full_run` passes there only if it passed
- **AND** a skip of a non-slow check in a full run still fails the run
- Check: `report.slow_tier_rule`
