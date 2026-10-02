# Spec Delta: harness-reporting

## Purpose
One headless command runs every check and produces a machine-readable result and a human summary, so verifiers and future sessions can judge state without reading logs.

## ADDED Requirements

### Requirement: Single-command full run
`harness/hs run` SHALL run every registered check (optionally filtered by `--only <glob>`), bring the stack up as needed, and exit 0 only if every executed check passed. A full run (no filter) SHALL include every check referenced by any scenario in the archived specs and in the active changes, which are the changes listed in `harness/active-changes` (those being built or verified). Drafted changes not yet listed are reported but don't gate the run.

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

### Requirement: Results schema
`results.json` SHALL contain: run metadata (start/end time, git SHA of this repo, `fork.lock` SHAs, host info) and, per check, `id`, `status` (`pass`|`fail`|`error`|`skip`), `duration_s`, and `evidence` (commands run, key output excerpts, expected vs. actual).

#### Scenario: Schema is valid
- **GIVEN** a completed run
- **WHEN** `results.json` is validated against `harness/results.schema.json`
- **THEN** it validates, and every check record has non-empty evidence
- Check: `report.schema`
