# dungeon-scaling Specification

## Purpose
TBD - created by archiving change m4-dungeon-scaling. Update Purpose after archive.

## Requirements

### Requirement: The scaling module is part of every build
Every server build SHALL include mod-autobalance at the commit pinned in `fork.lock`, and every started worldserver SHALL run it with Moonglade's AutoBalance settings. A release SHALL carry the module's source revision and licence notice.

#### Scenario: Module built in and configured
- **GIVEN** a server built by `harness/hs build` from a clean tree
- **WHEN** the stack starts with the default profile
- **THEN** the core's `modules/` holds a harness symlink to the pinned mod-autobalance checkout at exactly the `fork.lock` commit; the worldserver log shows the module's config load; `.ab getoffset` (typed by a probe) answers instead of "Command does not exist"; and every AutoBalance key that Moonglade sets has its Moonglade value in the running config (read through `.ab mapstat` in an instance, or the rendered conf)
- Check: `scale.module_loaded`

#### Scenario: Release carries the module
- **GIVEN** a release payload built by `tools/release`
- **WHEN** its third-party notices and source manifest are read
- **THEN** mod-autobalance appears with its pinned commit, its licence notice (the GPL-2.0-or-later header from `src/AutoBalance.cpp`) and its source tree, and the module's config file is in the payload
- Check: `scale.release_notice`

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
- **WHEN** the world is paused for 20 s with the true-pause addon message and resumed
- **THEN** the boss's maximum health and the reported multiplier are unchanged
- Check: `scale.pause_safe`

### Requirement: Each listed dungeon's final boss can be beaten alone
With Moonglade's AutoBalance settings, a single character at each listed dungeon's target level SHALL be able to beat that dungeon's final boss alone. The character uses the playerbots autogear at the configured quality limit and is driven by the playerbots "self" AI. The settings are tuned by these runs, and the measured results are recorded in `docs/research/m4-scaling-tuning.md`.

#### Scenario: Solo final boss, every listed dungeon
- **GIVEN** for each listed dungeon (The Deadmines, Razorfen Downs, Hellfire Ramparts, Utgarde Keep, normal difficulty), the probe character set to the dungeon's target level, auto-geared, GM mode off, alone, with the playerbots self-AI on, placed by GM teleport near the final boss's spawn in a fresh copy
- **WHEN** the probe engages the boss
- **THEN** within 10 minutes and at most 2 attempts (a wipe resets the boss), the boss dies, the dungeon's final encounter bit is set in the instance's completed-encounter mask, and the evidence holds the boss's scaled health, the character's deaths and the fight length
- Check: `scale.solo_final_boss`

#### Scenario: The solo fight feels right
- **GIVEN** the tuned settings
- **WHEN** Zach fights a listed dungeon's bosses alone at about the dungeon's level
- **THEN** the bosses are beatable with careful play and feel like a real fight, neither trivial nor impossible
- [human-verify] Fight feel can't be measured by a script. It is judged at the M4 human gate, and the answer feeds the default difficulty question in the proposal.
