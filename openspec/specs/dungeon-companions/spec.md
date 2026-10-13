# dungeon-companions Specification

## Purpose
TBD - created by archiving change m4-party-preset. Update Purpose after archive.

## Requirements

### Requirement: A dungeon party in one command
The Moonglade addon SHALL provide `/mg party`. It fills the player's party to five with companions covering the roles the player's own talent spec doesn't (by default a tank, a healer and two damage dealers), and `/mg party clear` removes them. Each companion SHALL join within 3 levels of the player, in its role's talent spec, auto-geared, with its role's strategies. The slash command SHALL send only stock mod-playerbots commands, taken from one table in `MoongladeParty.lua`.

#### Scenario: Party preset fills the roles
- **GIVEN** the probe character (a damage-dealing spec) at a listed dungeon's target level, with no party
- **WHEN** the probe sends, in order, the command list parsed from `MoongladeParty.lua` (the same table the slash command uses)
- **THEN** within 90 s the probe's party lists 4 online companions. Exactly one has the `tank` combat strategy and one has `heal` or `holy heal` (read by whispering `co ?` to each and parsing the reply). The two others have `dps assist`, `threat` and `wait for attack`. Every companion's level is within 3 of the probe's, and every one has all its armour slots filled except shirt and tabard (read from `character_inventory`)
- Check: `dng.party_preset`

#### Scenario: Role-aware fill
- **GIVEN** the probe character in a protection (tank) spec
- **WHEN** it sends the party command list
- **THEN** no companion has the `tank` strategy, one has a healing strategy, and three are damage dealers
- Check: `dng.party_preset_tank_player`

#### Scenario: The slash command sends the plan
- **GIVEN** the addon loaded under the Lua stubs, a player of each role (damage dealer, tank, healer)
- **WHEN** `/mg party` and then `/mg party clear` are run
- **THEN** the commands sent are exactly the plan's for that role, in order (addclass, then each companion's spec, autogear and strategy whispers), and `clear` sends the stock removal command; an unknown role sends nothing and says why
- Check: `unit:lua:test_addon_party.damage_dealer_gets_tank_healer_mage_rogue`
- Check: `unit:lua:test_addon_party.tank_player_gets_a_hunter_instead_of_a_tank`
- Check: `unit:lua:test_addon_party.healer_player_gets_a_tank_and_a_hunter`
- Check: `unit:lua:test_addon_party.steps_wait_for_the_companions_answer`
- Check: `unit:lua:test_addon_party.clear_sends_the_stock_removal`
- Check: `unit:lua:test_addon_party.unknown_role_sends_nothing_and_says_why`
- [human-verify] The real client's party frame filling is seen by Zach at the M4 gate. Reason: the command plan is unit-tested, the server's response is checked by `dng.party_preset`, and a real-client GUI run would cost minutes on every PR for no extra coverage.

### Requirement: AutoBalance's chat lines stay hidden
The player SHALL NOT see AutoBalance's "[AutoBalance] Combat has ended" system message (`ABPlayerScript.cpp:224`), which fires when a party member leaves mid-fight. mod-moonglade filters it; scaling itself is unchanged.

#### Scenario: The filter drops only AutoBalance lines
- **GIVEN** the filter's logic (pure function over a message)
- **WHEN** it sees the AutoBalance combat-ended line, another AutoBalance line, and an ordinary system message
- **THEN** both AutoBalance lines are dropped and the ordinary message passes
- Check: `unit:cpp:chat_filter.drops_combat_ended_line`
- Check: `unit:cpp:chat_filter.drops_other_autobalance_lines`
- Check: `unit:cpp:chat_filter.ordinary_messages_pass`

#### Scenario: Login text stays clean
- **GIVEN** the login text word list
- **WHEN** `client.login_text_clean` runs
- **THEN** "AutoBalance" is one of the words it fails on
- Check: `client.login_text_clean`

### Requirement: Dungeon tactics switch on by themselves
In a dungeon that mod-playerbots has tactics for, each companion SHALL have that dungeon's instance strategy while it is inside, and SHALL lose it after leaving.

#### Scenario: Utgarde Keep and Hellfire Ramparts tactics
- **GIVEN** a `/mg party` companion party
- **WHEN** the party enters Utgarde Keep, then Hellfire Ramparts, then The Deadmines
- **THEN** inside Utgarde Keep every companion's `co ?` reply lists `wotlk-uk`; inside Ramparts every reply lists `tbc-ramp` and not `wotlk-uk`; inside the Deadmines neither is listed. The strategy names come from `PlayerbotAI::ApplyInstanceStrategies`'s map table, keyed by the map ids from LFGDungeons.dbc
- Check: `dng.instance_strategy`

### Requirement: The tank holds the monsters and the healer keeps the party alive
In a dungeon fight the tank companion SHALL hold the boss and the healer companion SHALL keep the party alive, well enough that the party finishes the listed dungeons.

#### Scenario: The roles carry a full clear
- **GIVEN** the probe and a `/mg party` companion party (warrior tank, priest healer, two damage dealers) at each listed dungeon's target level, with the dungeon at full five-player strength
- **WHEN** the run driver takes the party through every required encounter
- **THEN** every listed dungeon is finished with at most 2 wipes per encounter (the "Party clears each listed dungeon" scenario), and the evidence holds deaths per role for every pull
- Check: `dng.party_clear`

### Requirement: Companions step out of ground effects
A companion SHALL move out of a hostile persistent ground effect whose radius is within `AiPlayerbot.MaxAoeAvoidRadius`.

#### Scenario: Out of the fire
- **GIVEN** Zach in a listed dungeon with a `/mg party` party
- **WHEN** a boss or monster puts a damaging effect on the ground under a companion
- **THEN** the companion steps out of it within a few seconds
- [human-verify] This is stock playerbots behaviour (`AiPlayerbot.AutoAvoidAoe`, default on) that Moonglade doesn't change; a game test for it would be another slow end-to-end check of a third-party default. It is judged by playing at the M4 human gate.

### Requirement: Orders work in dungeons
Raid-icon focus and crowd-control orders from the player SHALL be obeyed by companions.

#### Scenario: Kill skull first
- **GIVEN** Zach in a listed dungeon with a `/mg party` party, next to a pack of monsters
- **WHEN** he marks one with the skull icon and whispers `rti skull` to the party and `tank attack` to the tank (cheat sheet)
- **THEN** the party kills the skull-marked monster first
- [human-verify] Playerbots' default `rti` value is already the skull (`RtiValue.cpp:10`), and the damage and tank targets prefer the rti target (`DpsTargetValue.cpp:283`, `TankTargetValue.cpp:112-128`), so a game test can't show the order matters: the party focuses the skull with or without it. A headless test also needs a pack with nothing else in sight, which The Deadmines doesn't have without clearing 13 spawns first (two live attempts, 2026-10-12). Judged by playing at the M4 human gate.

#### Scenario: Sheep the moon
- **GIVEN** Zach in a listed dungeon with a `/mg party` party that includes a mage
- **WHEN** he marks a humanoid in a pack with the moon icon and whispers `rti cc moon` to the mage before the pull (cheat sheet)
- **THEN** the mage polymorphs it and the party leaves it alone while it lasts
- [human-verify] Stock playerbots behaviour driven by the same raid-icon orders as "Kill skull first"; judged by playing at the M4 human gate.

### Requirement: Companions feel like a good party
Companions SHALL make dungeon runs smoother rather than more annoying.

#### Scenario: Zach runs a dungeon with companions
- **GIVEN** the stack is up with the M4 changes
- **WHEN** Zach runs a listed dungeon with a `/mg party` party
- **THEN** the tank leads and holds the monsters, the healer keeps up, nobody stands in effects for long, and the orders on the cheat sheet do what it says
- [human-verify] Party quality is judged by playing. It is checked at the M4 human gate.
