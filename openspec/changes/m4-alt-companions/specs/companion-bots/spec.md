## ADDED Requirements

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
- **THEN** the reply is the playerbots refusal (`PlayerbotMgr.cpp` "you can only add bots from your own account or linked accounts"), and the character stays offline
- Check: `alt.foreign_refused`

#### Scenario: Slash commands in the real client
- **GIVEN** a harness test character with an alt on the same account, logged in with the real client (GUI tier)
- **WHEN** `/mg bring <alt>` and then `/mg dismiss <alt>` are typed
- **THEN** the party frame shows the alt within 30 s, then shows no party within 30 s of dismissing
- Check: `client.bring_slash`

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
- **THEN** the alt's equipped item entries and talents are unchanged, and the `autogear` whisper gets the playerbots refusal. The counterexample, with `AutoGearCommandAltBots = 1` (profile `altgear-on`), changes at least one equipped item
- Check: `alt.gear_untouched`

### Requirement: Alts feel like party members
Bringing your own characters SHALL feel like having your RPG party along.

#### Scenario: Zach brings an alt
- **GIVEN** Zach has a second character
- **WHEN** he brings it along for a quest or a listed dungeon
- **THEN** it plays its class sensibly, keeps its gear, and he finds the progress it made afterwards when he logs it in
- [human-verify] Judged by playing at the M4 human gate.
