# companion-bots Specification

## Purpose
Let a solo player add AI party members of chosen classes who follow, fight, and quest with them.

## Requirements

### Requirement: Add a companion of a chosen class
A logged-in player SHALL be able to add a companion bot of a chosen class to their party with an in-game chat command, and remove it again.

#### Scenario: Add and remove a companion
- **GIVEN** the test character is logged in (headless probe)
- **WHEN** the probe sends the playerbots chat command that adds a bot of class Priest
- **THEN** within 30 s the server reports a party containing the test character and an online bot of class Priest, and the reverse command removes it from the party within 30 s
- Check: `comp.add_remove`

### Requirement: Companions follow and assist
Companions SHALL follow their master and engage the master's enemies.

#### Scenario: Companion follows the master
- **GIVEN** the test character has a companion
- **WHEN** the test character is moved at least 100 yards away within the same zone
- **THEN** within 60 s the companion is within 15 yards of the master (per the server's saved or live positions)
- Check: `comp.follows`

#### Scenario: Companion fights the master's target
- **GIVEN** the test character has a combat-class companion near a hostile creature of appropriate level
- **WHEN** the master enters combat with that creature
- **THEN** within 20 s the companion is in combat with the same creature, and the creature dies without the master acting further
- Check: `comp.assists`

### Requirement: Companions quest with the player
Companions SHALL accept shared quests and make progress on them.

#### Scenario: Shared quest accepted
- **GIVEN** the test character has a quest (selected by SQL: a starter-zone quest with a kill objective) and a companion of appropriate level
- **WHEN** the master shares the quest
- **THEN** the companion has the quest in its log within 30 s
- Check: `comp.quest_shared`

#### Scenario: Zach quests with companions
- **GIVEN** the stack is up
- **WHEN** Zach adds two companions and quests for about 15 minutes
- **THEN** the companions follow, fight, heal and loot sensibly, and they make questing easier rather than more annoying
- [human-verify] Companion quality is judged by playing. This is checked at the next human gate.

### Requirement: Assist check is stable
The harness check for "Companion fights the master's target" SHALL establish its precondition deterministically, so it doesn't fail because another unit (player, bot or creature) interferes with the chosen target.

#### Scenario: Fifteen consecutive passes
- **GIVEN** the m0 test world
- **WHEN** `comp.assists` runs 15 times in a row in isolation, and once in an unfiltered full tier
- **THEN** every run passes, every run's evidence shows the chosen creature wasn't in combat with any other unit before the fight, and the existing counterexamples (passive companion, companion not near, master attacks) still fail
- Check: `comp.assists`
