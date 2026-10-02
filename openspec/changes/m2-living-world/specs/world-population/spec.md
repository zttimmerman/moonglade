# Spec Delta: world-population

## Purpose
Make the world feel inhabited: AI players visibly present and active in every starting zone and capital, without a human anywhere near them.

## ADDED Requirements

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
