## ADDED Requirements

### Requirement: A save holds one character's progress
A save SHALL hold exactly one character's progress, as the save set in design.md section 1 defines it: level, XP, talents and spells, gear, bags, bank, gold, quests, reputation, professions, pets and position. Loading a save SHALL restore that progress under the same character ID. It SHALL NOT change any other character, any AI player, the world, the auction house, guilds, friends, instance lockouts or the mailbox. Every table in the character database that has a character or item column SHALL be classified as in the save, deliberately kept, or removed on load.

#### Scenario: Save and load round trip
- **GIVEN** a test-account fixture hunter with a pet, out of the world, with known level, XP, gold, equipped items, items in bags and bank, an equipment set, an active quest, a talent, a profession skill and a reputation value, and a save taken of it
- **WHEN** the character then changes each of these (levels up, spends gold, destroys and gains items, abandons the quest, resets talents, gains reputation, moves zone), and the save is loaded
- **THEN**
  - every row in the save set equals the save's content. Items, pets and equipment sets are compared by content, not ID;
  - the position is within 0.01 yards of the saved one;
  - the character ID and name are unchanged;
  - the probe logs in and the server reports the restored level, gold and quest state
- Check: `saves.roundtrip`

#### Scenario: Loading touches nothing else
- **GIVEN** two test-account characters A and B, a mail in A's mailbox, an auction by A, A on B's friend list, and a checksum of every other table in the character and playerbots databases taken before the load
- **WHEN** a save of A is loaded
- **THEN**
  - B's rows, A's mail, the auction and its item, the friend entry, A's instance lockouts and every table outside the save set are byte-identical to before;
  - the only rows that changed are A's save-set rows and A's corpse, if any
- Check: `saves.scope`

#### Scenario: Items that left after the save don't collide
- **GIVEN** a save of character A holding item X, after which A mails X to character B
- **WHEN** A's save is loaded
- **THEN**
  - A has item X again under a new item ID;
  - B's mail still holds the original X;
  - no primary key is duplicated;
  - the server log shows no item or DB error;
  - both characters log in
- Check: `saves.item_ids_fresh`

#### Scenario: Every character table is classified
- **GIVEN** the running character database
- **WHEN** the check lists every table with a column naming a character (`guid`, `owner`, `owner_guid`, `receiver`, `player*`) or an item
- **THEN** each one is in the module's in-save, kept or removed-on-load list, and the in-save list matches design.md section 1. A table on no list fails the check
- Check: `saves.table_coverage`

### Requirement: Saving: manual, quick and automatic
A player character SHALL be able to make manual saves (optionally named) and one quicksave from the game, and SHALL get autosaves when entering a rest area, taking a flight and arriving on a new map, at most one per `Moonglade.Saves.AutoMinIntervalS`. Saves SHALL be refused, with a message and nothing written, while the character is in combat, dead or a ghost, falling, in a battleground or arena, or between maps. Bot sessions SHALL never save. Rotation SHALL keep 5 autosaves, 1 quicksave and 3 before-load saves per character, and all manual saves.

#### Scenario: Manual save
- **GIVEN** a probe character in the world, out of combat
- **WHEN** it sends the `MGSAVE SAVE manual <name>` addon message
- **THEN**
  - within 5 s a complete save folder exists under the test account's store, with a manifest naming the character, level, zone and the given name;
  - the file's sha256 matches the manifest;
  - the probe receives a success reply line;
  - no `.tmp-*` folder is left
- Check: `saves.manual`

#### Scenario: One quicksave slot
- **GIVEN** a character with an existing quicksave
- **WHEN** it quicksaves twice more
- **THEN** exactly one quicksave exists for the character, and it is the newest
- Check: `saves.quick_slot`

#### Scenario: Autosaves at safe moments, rotated
- **GIVEN** a probe character with the autosave interval set short (test config)
- **WHEN** it enters an inn, takes a flight, and teleports to another map, and then repeats until more than 5 autosaves have been triggered
- **THEN**
  - each trigger produced one autosave, labelled with its moment;
  - triggers inside the interval produced none;
  - only the 5 newest autosaves remain;
  - manual saves were not rotated
- Check: `saves.autosave_triggers`

#### Scenario: No saving in combat or while dead
- **GIVEN** a probe character in melee with a creature, and later the same character dead
- **WHEN** it asks for a manual save and a quicksave, and an autosave trigger fires during the fight
- **THEN**
  - no save folder is created;
  - the character gets the refusal message ("You can't save in combat." / "… while dead.");
  - the autosave is skipped and not queued (none appears after combat ends)
- Check: `saves.refused_in_combat`

#### Scenario: Companions never save
- **GIVEN** a test-account alt logged in as a playerbot companion, which passes autosave triggers (zone change with its master)
- **WHEN** the triggers fire
- **THEN** no save appears for the alt, and the master's own autosave happens as normal
- Check: `saves.bots_skip`

#### Scenario: A killed save leaves nothing half-written
- **GIVEN** a save in progress, paused by the test switch after its first file
- **WHEN** the worldserver is killed (SIGKILL) and the stack started again
- **THEN**
  - no partial save is listed;
  - the `.tmp-*` folder is removed at start;
  - the previous saves still load
- Check: `saves.atomic`

### Requirement: Loading without restarting
A save SHALL be loadable from the character screen and from the in-game Saves window, with no restart of the game, the launcher or the server. Loading SHALL first take a before-load save of the current state. It SHALL be all or nothing, SHALL run only when the character is out of the world, and SHALL be applied after every earlier write of that character. Loading SHALL be refused, with a reason and nothing changed, for a damaged save, a save of another character or account, a save from a different database schema, or a character that is in the world (including as a companion).

#### Scenario: Load in the game
- **GIVEN** a probe character in the world, in combat, with a save from an earlier level
- **WHEN** it sends `MGSAVE LIST`, then `MGSAVE LOAD <generation> <index>` naming that save from the list
- **THEN**
  - the probe gets `SMSG_LOGOUT_COMPLETE` within 3 s, without a 20 s countdown;
  - the next character list shows the saved level and zone;
  - entering the world places the character at the saved position;
  - the worldserver pid is unchanged
- Check: `saves.inworld_load`

#### Scenario: Earlier writes can't overwrite a load
- **GIVEN** a character that just moved and gained gold (a periodic save pending), and `CharacterDatabase.WorkerThreads` read from the rendered config
- **WHEN** a load is requested in the game 0.1 s later, 20 times in a row
- **THEN**
  - every time, the database afterwards holds the save's position and gold, not the pre-load values;
  - the module refuses to enable saves when the test config sets `WorkerThreads = 2` (logged reason)
- Check: `saves.load_ordering`

#### Scenario: Load at the character screen
- **GIVEN** the play client at the character screen in a test run, with the listener up and a character with two saves
- **WHEN** the script opens Saves, picks the older save and confirms
- **THEN**
  - within 10 s the character list shows the saved level;
  - the notice names the loaded save;
  - the Wow.exe, launcher and worldserver pids are the same as before;
  - no stock connecting or error dialog was seen;
  - pressing Play enters the world at the saved position
- Check: `saves.live_charselect_load`

#### Scenario: A load can be undone
- **GIVEN** a character that has just loaded an older save
- **WHEN** its "Before loading" save is loaded
- **THEN** the character's save-set rows equal those from before the first load (compared by content), and at most 3 before-load saves exist for it
- Check: `saves.undo`

#### Scenario: Load is all or nothing
- **GIVEN** a load with the test switch set to fail the transaction after half its statements
- **WHEN** the load runs
- **THEN** the character's rows are identical to before (checksums), the load reports failure, and the before-load save exists
- Check: `saves.load_atomic`

#### Scenario: Refused loads change nothing
- **GIVEN** one save each that is damaged (one byte changed), of another character, of another account, or with an altered schema fingerprint, plus a target character that is online (as itself, and as a companion)
- **WHEN** each load is requested
- **THEN** each is refused with its own reason, no rows change (checksums), and no before-load save is written for a refused load
- Check: `saves.load_refusals`

### Requirement: The save lists
The character screen SHALL show, for the selected character, every save of that character, including saves made earlier in the same game session, newest first, labelled in plain language. The in-game Saves window SHALL show the same list. Labels SHALL name the place, level, kind and time, never an ID, a path, or words such as server, realm or account. Quicksave and quickload SHALL be key bindings of the Moonglade add-on.

#### Scenario: A save made in the game shows at the character screen
- **GIVEN** a test run that entered the world, made a manual save and a quicksave, and exited to the character screen
- **WHEN** the script opens Saves for that character
- **THEN** the panel lists both new saves at the top with their labels, without restarting the game
- Check: `saves.live_charselect_list`

#### Scenario: The in-game list
- **GIVEN** a probe character with manual, quick, auto and before-load saves
- **WHEN** it sends `MGSAVE LIST`
- **THEN** it receives one reply line per save, newest first, each ≤ 255 bytes, with kind, label, time, level and zone, and the same set the store holds
- Check: `saves.inworld_list`

#### Scenario: Wording and secrets
- **GIVEN** the generated save list for the glue and the add-on's reply lines from a test store
- **WHEN** they are scanned
- **THEN**
  - no save id, path, nonce or password appears;
  - no label contains "server", "realm", "account" or "database";
  - the glue file is 0600 inside the play client only
- Check: `saves.list_wording`

#### Scenario: The add-on ships the menu and the keys
- **GIVEN** the play client's Moonglade add-on
- **WHEN** its files are read
- **THEN**
  - `Bindings.xml` declares quicksave and quickload;
  - the default keys are free in the client's own default bindings (read from the client's files, cited in evidence);
  - the Esc menu has a Saves button
- Check: `saves.addon_static`

#### Scenario: It feels like an Elder Scrolls save system
- **GIVEN** Zach playing a character
- **WHEN** he quicksaves, gets into trouble, quickloads, loads an older save from the character screen, and then plays an alt
- **THEN** each load feels quick and clear, the alt is unaffected, and the wording makes sense
- [human-verify] Whether it feels right is Zach's judgement; the mechanics are covered by the checks above

### Requirement: Saves are stored apart from the live database, per lane and account
Saves SHALL be written under `<lane env>/saves/<ACCOUNT>/<character id>/` (the player's in `env/saves/`), atomically, with a manifest. Saves SHALL never be written into `client/`. Test runs and lane B SHALL never create, change or delete anything under the player's account folder.

#### Scenario: The player's saves are untouched by tests
- **GIVEN** a snapshot (names, sizes, sha256) of `env/saves/<PLAYER>/` before a harness run
- **WHEN** the run finishes
- **THEN** the snapshot is identical, and no lane-B process opened anything under `env/saves/`
- Check: `saves.player_untouched`
