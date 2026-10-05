# Spec Delta: companion-bots

## ADDED Requirements

### Requirement: Assist check is stable
The harness check for "Companion fights the master's target" SHALL establish its precondition deterministically, so it doesn't fail because another unit (player, bot or creature) interferes with the chosen target.

#### Scenario: Fifteen consecutive passes
- **GIVEN** the m0 test world
- **WHEN** `comp.assists` runs 15 times in a row in isolation, and once in an unfiltered full tier
- **THEN** every run passes, every run's evidence shows the chosen creature wasn't in combat with any other unit before the fight, and the existing counterexamples (passive companion, companion not near, master attacks) still fail
- Check: `comp.assists`
