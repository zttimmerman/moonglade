## ADDED Requirements

### Requirement: The tank holds the monsters
In a dungeon fight the tank companion SHALL take the boss's attacks, and damage-dealing companions SHALL NOT pull it off.

#### Scenario: Boss attacks land on the tank
- **GIVEN** the probe and a `/mg party` companion party at the dungeon's target level in a fresh Utgarde Keep (normal), staged out of combat near the first encounter's boss (the encounter with the lowest `orderIndex` in DungeonEncounter.dbc)
- **WHEN** the probe selects the boss and whispers `tank attack` to the tank, and the party fights it
- **THEN** from 5 s after the first boss swing until the boss dies, at least 80% of the boss's melee swings (`SMSG_ATTACKERSTATEUPDATE`, attacker = boss) hit the tank. The evidence holds the swing counts per victim. Without the tank (counterexample: the party with the tank removed), the same measure is below 80%
- Check: `dng.tank_holds`

### Requirement: The healer keeps the party alive
The healer companion SHALL heal party members who take damage in a dungeon fight.

#### Scenario: Heals land during a boss fight
- **GIVEN** the same staged fight as "Boss attacks land on the tank"
- **WHEN** the fight runs to the boss's death
- **THEN** the healer cast at least one heal on the tank (`SMSG_SPELLHEALLOG`, caster = healer, target = tank), no party member died, and the evidence holds the heal count, the total healed and each member's lowest health percentage
- Check: `dng.healer_heals`

### Requirement: Companions step out of ground effects
A companion SHALL move out of a hostile persistent ground effect (a dynamic-object area aura) whose radius is within `AiPlayerbot.MaxAoeAvoidRadius`.

#### Scenario: Out of the fire
- **GIVEN** a companion in combat with a hostile creature, where the creature's victim is the companion
- **WHEN** the GM probe makes the creature cast, at its victim (`.cast target <spell>`), a hostile persistent area spell chosen by rule: Spell.dbc has an effect `SPELL_EFFECT_PERSISTENT_AREA_AURA` with a periodic-damage aura, the radius is at most `MaxAoeAvoidRadius`, the spell isn't in `AoeAvoidSpellWhitelist`, and it has the lowest ID among those whose dynamic object lasts at least 10 s
- **THEN** within 5 s the companion is farther from the effect's centre than its radius. With `AiPlayerbot.AutoAvoidAoe = 0` (counterexample profile `aoe-avoid-off`), it is still inside after 5 s
- Check: `dng.avoid_aoe`

### Requirement: Dungeon tactics switch on by themselves
In a dungeon that mod-playerbots has tactics for, each companion SHALL have that dungeon's instance strategy while it is inside, and SHALL lose it after leaving.

#### Scenario: Utgarde Keep and Hellfire Ramparts tactics
- **GIVEN** a `/mg party` companion party
- **WHEN** the party enters Utgarde Keep, then Hellfire Ramparts, then The Deadmines
- **THEN** inside Utgarde Keep every companion's `co ?` reply lists `wotlk-uk`; inside Ramparts every reply lists `tbc-ramp` and not `wotlk-uk`; inside the Deadmines neither is listed. The strategy names come from `PlayerbotAI::ApplyInstanceStrategies`'s map table, keyed by the map ids from LFGDungeons.dbc
- Check: `dng.instance_strategy`

### Requirement: Orders work in dungeons
Raid-icon focus and crowd-control orders from the player SHALL be obeyed by companions.

#### Scenario: Kill skull first
- **GIVEN** a companion party next to a pack of at least two hostile creatures in a dungeon (pack chosen by rule: the hostile, non-boss `creature` spawn nearest the first boss's staging point that has at least one other hostile spawn within 10 yd, together with those neighbours)
- **WHEN** the probe marks one creature with the skull icon (`MSG_RAID_TARGET_UPDATE`), whispers `rti skull` to the party and `tank attack` to the tank
- **THEN** that creature dies first, and before it dies at least 80% of the damage-dealing companions' damage events land on it
- Check: `dng.rti_focus`

#### Scenario: Sheep the moon
- **GIVEN** the same kind of pack, with a mage companion
- **WHEN** the probe marks a humanoid creature in the pack (creature_template `type` 7) with the moon icon and whispers `rti cc moon` to the mage before the pull
- **THEN** within 15 s of the pull the moon-marked creature carries the mage's polymorph aura (a Spell.dbc spell whose effect applies `SPELL_AURA_TRANSFORM` and that the mage knows), and no companion damages it while the aura lasts
- Check: `dng.cc_on_mark`

### Requirement: Companions feel like a good party
Companions SHALL make dungeon runs smoother rather than more annoying.

#### Scenario: Zach runs a dungeon with companions
- **GIVEN** the stack is up with the M4 changes
- **WHEN** Zach runs a listed dungeon with a `/mg party` party
- **THEN** the tank leads and holds the monsters, the healer keeps up, nobody stands in effects for long, and the orders on the cheat sheet do what it says
- [human-verify] Party quality is judged by playing. It is checked at the M4 human gate.
