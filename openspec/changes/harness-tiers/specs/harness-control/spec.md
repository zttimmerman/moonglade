# Spec Delta: harness-control

## ADDED Requirements

### Requirement: Test account logs out instantly
The harness test account SHALL hold the instant-logout permission and no other elevated permission. Accounts not created by the harness (e.g. the player's own) SHALL NOT be changed.

#### Scenario: Test character logout has no timer
- **GIVEN** the test character logged in through the probe, not in a rest area
- **WHEN** the probe requests logout
- **THEN** the server answers with an instant logout (no 20-second countdown), and the test account's security level is still 0 (not a GM)
- Check: `fixture.instant_logout`
