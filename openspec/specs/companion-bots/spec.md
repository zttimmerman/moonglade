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

### Requirement: Your own characters as companions
A logged-in player SHALL be able to bring another character from their own account into their party as an AI companion, and dismiss it again, with `/mg bring <name>` and `/mg dismiss <name>` (stock `.playerbots bot add|remove`). A character on a different account SHALL be refused.

#### Scenario: Bring and dismiss an alt
- **GIVEN** the harness companion account holds the probe character and a second, offline character (the "alt", created by the check with `CMSG_CHAR_CREATE`)
- **WHEN** the probe sends the `/mg bring` command list for the alt (parsed from the addon's command table), and later the `/mg dismiss` list
- **THEN**:
  - within 30 s the alt is online and in the probe's party, and `characters.account` for the alt is unchanged;
  - after dismiss, within 30 s the alt is out of the party (`SMSG_GROUP_LIST`), and within 60 s it is offline.
- Check: `alt.bring_dismiss`

#### Scenario: Another account's character is refused
- **GIVEN** a character that belongs to a different harness account (the HSTEST fixture account's character), offline
- **WHEN** the probe sends `.playerbots bot add <that name>`
- **THEN** the reply is the playerbots refusal for `add` (`PlayerbotMgr.cpp:110-114`, "You are not allowed to control bot <name>"; the "own account or linked accounts" text belongs to the `addaccount` path, :702-704), and the character stays offline
- Check: `alt.foreign_refused`

#### Scenario: The slash commands send the right command lists
- **GIVEN** the Moonglade add-on loaded in the unit-test client stub
- **WHEN** `/mg bring <alt>` and then `/mg dismiss <alt>` are typed
- **THEN** the add-on sends exactly the bring list (`.playerbots bot add <alt>`, then, once the alt has joined, the `reset botAI` whisper, resent until answered; a whisper to an offline character is lost) and then `.playerbots bot remove <alt>`, and a missing name prints the usage line without sending anything. The game side of the same lists is `alt.bring_dismiss`
- Check: `unit:lua:test_addon_bring.bring_and_dismiss_send_the_command_lists`

### Requirement: An alt keeps what it earns
Progress an alt makes as a companion (XP, levels, loot, money, item durability) SHALL be saved to that character as if played. Loading a per-character save of the master SHALL NOT change the alt.

#### Scenario: XP and loot stay with the alt
- **GIVEN** the alt (a level-appropriate character for the comp.assists creature rule) brought in as a companion, with its XP, money and inventory recorded from the characters DB
- **WHEN** the party kills at least two creatures chosen by the comp.assists creature rule, the alt is dismissed, and the master loads a per-character save made before the trip (front-menu-saves)
- **THEN** the alt's `characters.xp` (or level) is higher than before, its money and inventory reflect the trip (any looted item is present), and none of the alt's rows changed when the master's save was loaded (rows compared before and after the load)
- Check: `alt.progress_kept`

### Requirement: An alt's gear and talents are protected
By default, the companion system SHALL NOT replace an alt's equipped items or change its talents. `AiPlayerbot.AutoGearCommandAltBots` SHALL be 0, and the `/mg party` and `/mg bring` command lists SHALL NOT send `autogear`, `init` or `talents` commands to an alt.

#### Scenario: Gear and talents unchanged
- **GIVEN** the alt with its equipped items (`character_inventory` slots 0–18 item entries) and talents (`character_talent`) recorded
- **WHEN** it is brought in, `/mg party` runs alongside it, the master whispers it `autogear`, and the party fights one creature
- **THEN** the alt's equipped item entries and talents are unchanged, and the `autogear` whisper gets the playerbots refusal
- Check: `alt.gear_untouched`

#### Scenario: The setting that protects alts is on
- **GIVEN** the profiles Moonglade ships and the `/mg party` and `/mg bring` command tables
- **WHEN** the profile configs are rendered and the tables are read
- **THEN** every shipped profile sets `AiPlayerbot.AutoGearCommandAltBots = 0`, and no command list sent to an alt contains `autogear`, `init` or `talents`
- Check: `unit:py:tests.unit.test_alt_config.AltConfig.test_alt_gear_protected_in_every_profile`

### Requirement: Alts feel like party members
Bringing your own characters SHALL feel like having your RPG party along.

#### Scenario: Zach brings an alt
- **GIVEN** Zach has a second character
- **WHEN** he brings it along for a quest or a listed dungeon
- **THEN** it plays its class sensibly, keeps its gear, and he finds the progress it made afterwards when he logs it in
- [human-verify] Judged by playing at the M4 human gate.
