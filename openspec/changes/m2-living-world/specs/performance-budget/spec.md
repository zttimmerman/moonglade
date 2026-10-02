# Spec Delta: performance-budget

## Purpose
Keep the whole stack (database, servers, bots, and the client) playable on the target Mac: Apple M2 Pro, 16 GB RAM.

## ADDED Requirements

### Requirement: Server-side budget under the population profile
With the population profile active, measured over a 10-minute window after warm-up, the stack SHALL stay within these limits: worldserver resident memory ≤ 4.0 GB; mysqld resident memory ≤ 1.0 GB; the server's world-update time p95 ≤ 100 ms and median ≤ 50 ms (read from the server's own reporting); host free plus inactive memory ≥ 3.0 GB, leaving room for the client.

#### Scenario: Budget met with population running
- **GIVEN** the population profile is active and warm-up has finished
- **WHEN** the harness samples process memory, host memory, and world-update time every 10 s for 10 minutes
- **THEN** every limit above holds, and the evidence includes all samples and the population size N
- Check: `perf.server_budget`

#### Scenario: Population size is the largest that fits
- **GIVEN** the budget measurement passes at population N
- **WHEN** the tuning record in the change is consulted
- **THEN** it shows a measured run at a larger population (e.g. 1.5×N) that exceeded at least one limit, or states that N is the playerbots config ceiling
- Check: `perf.population_tuned`

### Requirement: Client-side playability
The client SHALL stay smooth while the populated server runs on the same Mac.

#### Scenario: Zach plays in a populated capital
- **GIVEN** the population profile is active
- **WHEN** Zach plays for 10 minutes in a capital city and in a starting zone
- **THEN** the game feels smooth (no sustained stutter or long hitches) and the Mac stays responsive
- [human-verify] Frame pacing is perceptual, and there's no reliable headless FPS readout under Wine/mtld3d. If mtld3d exposes frame-time stats, add an automated check later and record it in the spec-change log.
