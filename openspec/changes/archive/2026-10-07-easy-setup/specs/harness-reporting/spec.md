# Spec Delta: harness-reporting

## ADDED Requirements

### Requirement: Only allowlisted checks may claim build time
The timing guard SHALL subtract self-reported build time (`evidence.build_s`) only for checks on a reviewed allowlist (today `build.clean` and `core.login_logout_race_baseline`). Build time claimed by any other check SHALL count as check time and SHALL be listed as a warning.

#### Scenario: A non-allowlisted check can't hide a slowdown as build time
- **GIVEN** synthetic timing records against a known baseline
- **WHEN** a non-allowlisted baseline check is 600 s slower and claims 600 s of `build_s`, and separately the allowlisted race baseline claims its real rebuild time
- **THEN** the first fails the guard and warns about the claim; the second passes with the build time reported
- Check: `report.timing`
