## ADDED Requirements

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
- [human-verify] Playerbots' default `rti` target is already the skull (`RtiTargetValue`), so a game test can't show the order matters: the party focuses the skull with or without it. A headless test also needs a pack with nothing else in sight, which The Deadmines doesn't have without clearing 13 spawns first (two live attempts, 2026-10-12). Judged by playing at the M4 human gate.

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
