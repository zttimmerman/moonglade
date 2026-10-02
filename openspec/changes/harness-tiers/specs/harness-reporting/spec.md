# Spec Delta: harness-reporting

## ADDED Requirements

### Requirement: Test tiers
Every check SHALL declare a tier: `quick`, `full`, or `slow`. `harness/hs run --tier quick` SHALL run only `quick` checks. `--tier full` (the default) SHALL run `quick` and `full` checks. `--tier slow` SHALL run all checks. `results.json` SHALL record the tier.

#### Scenario: Quick tier runs fast and says so
- **GIVEN** a built server
- **WHEN** `harness/hs run --tier quick` runs
- **THEN** it executes every `quick` check and no `full`/`slow` check, finishes in under 3 minutes on the target Mac, records `tier: quick` in results.json, and `report.full_run` reports fail ("not a full run")
- Check: `report.tiers`

#### Scenario: Every check has a tier
- **WHEN** the harness lists registered checks
- **THEN** each has exactly one tier from {quick, full, slow}, and `build.clean`, `client.launch_smoke` and `lifecycle.persist` are not `quick`
- Check: `report.tiers_declared`

## MODIFIED Requirements

### Requirement: Single-command full run
`harness/hs run` SHALL run every registered check in the selected tier (default `full`), optionally filtered by `--only <glob>`, bring the stack up as needed, and exit 0 only if every executed check passed. A run counts as **full** only if it is unfiltered and its tier is `full` or `slow`, and it SHALL include every check referenced by any scenario in the archived specs and in the active changes (those listed in `harness/active-changes`).

#### Scenario: Full run produces reports
- **GIVEN** a built server
- **WHEN** `harness/hs run` runs
- **THEN** `harness/results.json` and `harness/summary.md` are written, and the exit code is 0 iff every check's status is `pass`
- Check: `report.full_run`

#### Scenario: Every spec scenario maps to a check
- **GIVEN** the specs under `openspec/specs/` and the spec deltas of every change listed in `harness/active-changes`
- **WHEN** the harness runs its coverage self-check
- **THEN** every scenario has either a `Check: <id>` naming a registered check, or a `[human-verify]` tag with a reason; any other scenario makes this check fail and is listed
- Check: `report.coverage`
