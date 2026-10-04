# world-population Specification

## Purpose
Make the world feel inhabited: AI players visibly present and active in every starting zone and capital, without a human anywhere near them.

## Requirements

### Requirement: Population in starting zones and capitals
With the population profile active and after a warm-up of at most 10 minutes from `hs up`, random bots SHALL be online in every race's starting zone and every capital city. The zone set comes from the client's DBC/`game_tele` data at runtime.

#### Scenario: Every starting zone and capital has bots
- **GIVEN** the stack has been up with the population profile for the warm-up period
- **WHEN** the harness saves all players and reads online random-bot positions, mapping each to its zone via the DBC data
- **THEN** each of the 8 race starting zones and each of the 8 capital cities has at least 3 online random bots, and the evidence lists the per-zone counts and the source of each zone ID
- Check: `pop.zones_covered`

#### Scenario: Population is the configured size and stays online
- **GIVEN** the population profile sets N random bots
- **WHEN** the harness counts online random bots at the end of the warm-up and again 5 minutes later
- **THEN** both counts are at least 95% of N, and none of the bots logged a fatal error
- Check: `pop.size_stable`

### Requirement: Bots are active, not parked
Ambient bots SHALL move and act even with no real player nearby.

#### Scenario: Bots move and gain experience
- **GIVEN** the warm-up has finished
- **WHEN** the harness samples every online random bot's position and XP/level, waits 5 minutes, and samples again
- **THEN** at least 60% of bots changed position by more than 20 yards, and at least 25% gained XP, a level, or completed a quest
- Check: `pop.bots_active`

#### Scenario: Zach sees a living starting zone
- **GIVEN** the stack is up with the population profile
- **WHEN** Zach logs into a starting zone and a capital
- **THEN** they see bots moving around, fighting and questing, and they look plausible (names, gear, behaviour)
- [human-verify] "Feels populated" is a subjective experience the harness can't judge. This is checked at the next human gate.

### Requirement: World size is one easy setting
The population size SHALL be a single user setting with presets (`small`, `normal`, `large`, or a number) stored in a gitignored local settings file. `normal` (about 500 bots) is the default. `./play` SHALL start the living world at the configured size by default. Scaling down SHALL need only changing that setting (it's also intended as an option for the future front-menu screen). Harness checks keep their own fixed profiles and never read the user's setting.

#### Scenario: Scaling down works with one setting
- **GIVEN** the settings file sets the world size to `small`
- **WHEN** the stack starts the way `./play` starts it
- **THEN** the number of random bots online after warm-up is within 5% of the `small` preset's size, the population profile (not the 10-bot test profile) is active, and with no settings file the `normal` preset is used
- Check: `pop.size_setting`

### Requirement: Fresh bot world each session
Each population start SHALL reset random bots' levels and timers so they redistribute into starting zones and capitals (names, races and classes are kept). Only random-bot accounts are touched; player and harness accounts and characters are unchanged.

#### Scenario: Fresh start touches only bots
- **GIVEN** a population start
- **WHEN** the fresh-world reset runs
- **THEN** random-bot characters are back at the configured low levels, and checksums of all non-bot accounts and characters are identical before and after
- Check: `pop.fresh_start_scope`
