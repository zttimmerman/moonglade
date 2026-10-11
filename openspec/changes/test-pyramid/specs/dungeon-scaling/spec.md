## MODIFIED Requirements

### Requirement: Dungeon difficulty follows the party size
In a 5-player dungeon, creature health and damage SHALL scale with the number of players inside, companions included (they are players). The scaling SHALL be weakest for one player and SHALL reach the dungeon's unchanged values for a full party of five, with the curve set by Moonglade's AutoBalance settings (design.md §2). Characters in GM mode SHALL NOT count.

#### Scenario: Alone, the boss is weaker
- **GIVEN** a probe character at the dungeon's target level (LFGDungeons.dbc `Target_Level`), GM mode off, alone in a fresh copy of The Deadmines
- **WHEN** the probe reads the final boss's maximum health (selected by rule: the `instance_encounters` row whose `lastEncounterDungeon` is this dungeon's LFG id)
- **THEN** the boss's maximum health is below its unscaled maximum health, which is read from the same spawn under the `scaling-off` profile in the same run; and the ratio matches the multiplier `.ab creaturestat` reports for that boss to within 1%
- Check: `scale.solo_weaker`

#### Scenario: A full party meets the unchanged dungeon
- **GIVEN** the probe character and four companions (playerbots `addclass`) in a fresh copy of The Deadmines, out of combat
- **WHEN** the probe reads the final boss's maximum health
- **THEN** it equals the unscaled maximum health (read under the `scaling-off` profile) to within 1%, and `.ab mapstat` reports a player count of 5
- Check: `scale.party_stock`

#### Scenario: Each companion makes it tougher
- **GIVEN** a fresh copy of The Deadmines
- **WHEN** the probe reads the final boss's maximum health with 1, 3 and 5 players inside (probe plus 0, 2 and 4 companions, adjusted out of combat)
- **THEN** the three values strictly increase, and the 5-player value equals the unscaled value to within 1%
- Check: `scale.counts_companions`

#### Scenario: Scaling switched off
- **GIVEN** the `scaling-off` profile (AutoBalance disabled)
- **WHEN** the probe enters The Deadmines alone and reads the final boss's maximum health
- **THEN** it equals the health the core computes with no module involved, to within 1%. That health is `ceil(BaseHealth[expansion] × HealthModifier)` from `creature_classlevelstats` and `creature_template` (`CreatureData.h` `GenerateHealth`), times the rank's `Rate.Creature.*.HP` from the rendered worldserver.conf (`Creature::_GetHealthMod`, `Creature.cpp`)
- Check: `scale.disabled_stock`

#### Scenario: Outdoor creatures untouched
- **GIVEN** the default profile, with scaling on
- **WHEN** the probe reads the maximum health of an outdoor creature chosen by the comp.assists creature rule
- **THEN** it equals the value computed from the world DB (and, with vanilla mode on, from the era side table) exactly as without the module
- Check: `scale.outdoor_untouched`

#### Scenario: Pausing keeps the scaling
- **GIVEN** the probe alone in The Deadmines, the final boss scaled for one player
- **WHEN** the world is paused for 5 s with the true-pause addon message and resumed
- **THEN** the boss's maximum health and the reported multiplier are unchanged
- Check: `scale.pause_safe`
