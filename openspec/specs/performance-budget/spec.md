# performance-budget Specification

## Purpose
Keep the whole stack (database, servers, bots, and the client) playable on the target Mac: Apple M2 Pro, 16 GB RAM.

## Requirements

### Requirement: Server-side budget under the population profile
With the population profile active at the default size, measured over a 10-minute window after warm-up, the stack SHALL stay within these limits. Process memory is max(resident, physical footprint), because macOS swap makes resident memory undercount. The limits: worldserver ≤ 6.0 GB; mysqld ≤ 1.0 GB; worldserver + mysqld + authserver together ≤ 7.0 GB; the server's world-update time p95 ≤ 100 ms and median ≤ 50 ms (read from the server's own reporting).

#### Scenario: Budget met with population running
- **GIVEN** the population profile is active and warm-up has finished
- **WHEN** the harness samples process memory, host memory, and world-update time every 10 s for 10 minutes
- **THEN** every limit above holds, and the evidence includes all samples, the population size N, and host free+inactive memory (recorded, not limited: it depends on the user's other apps)
- Check: `perf.server_budget`

#### Scenario: The default fits and the headroom is measured
- **GIVEN** the budget measurement passes at the default (`normal`) population N
- **WHEN** the tuning record in `docs/research/m2-population-tuning.md` is consulted
- **THEN** it contains measured runs at N and at least one larger size, each with per-limit worst values, and it states the largest size measured and whether that size fit the budget
- Check: `perf.population_tuned`

### Requirement: Client-side playability
The client SHALL stay smooth while the populated server runs on the same Mac.

#### Scenario: Zach plays in a populated capital
- **GIVEN** the population profile is active
- **WHEN** Zach plays for 10 minutes in a capital city and in a starting zone
- **THEN** the game feels smooth (no sustained stutter or long hitches) and the Mac stays responsive
- [human-verify] Frame pacing is perceptual, and there's no reliable headless FPS readout under Wine/mtld3d. If mtld3d exposes frame-time stats, add an automated check later and record it in the spec-change log.
