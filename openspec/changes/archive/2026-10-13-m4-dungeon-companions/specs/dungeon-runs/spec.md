## ADDED Requirements

### Requirement: The listed dungeons can be finished with companions
Each listed dungeon (The Deadmines, Razorfen Downs, Hellfire Ramparts and Utgarde Keep, at normal difficulty) SHALL be finishable by one player plus a `/mg party` companion party at the dungeon's target level, with Moonglade's scaling settings. "Finished" means every required encounter is done. The required encounters are the dungeon's DungeonEncounter.dbc encounters whose credit creature has a static spawn on the map for that difficulty (the rule is in `m4-dungeon-scaling` design.md §6), and they always include the final encounter.

#### Scenario: Party clears each listed dungeon
- **GIVEN** for each listed dungeon:
  - the probe character at the target level (LFGDungeons.dbc `Target_Level`) with `initself` gear, GM mode off;
  - a `/mg party` companion party (command list from `MoongladeParty.lua`);
  - a fresh copy of the dungeon.
- **WHEN** the run driver takes the party to each required encounter in DungeonEncounter.dbc `orderIndex` order. It teleports the probe to the encounter's staging point (design.md §4), waits until the party is gathered and out of combat, and sends the pull.
- **THEN** within 45 minutes per dungeon, every required encounter's bit is set in the instance's completed-encounter mask (`characters.instance.completedEncounters`), with at most 2 wipes per encounter. A wipe means every party member dead; the driver revives the party at the staging point and retries. The evidence holds, per encounter: the time, wipes, deaths per role, and the boss's scaled health.
- Check: `dng.party_clear`

#### Scenario: Solo clear of The Deadmines
- **GIVEN** the probe character alone at The Deadmines' target level with `initself` gear, GM mode off, driven by the playerbots self-AI (`.playerbots bot self`), in a fresh copy
- **WHEN** the run driver takes it to each required encounter in order, as above
- **THEN** within 45 minutes every required encounter's bit is set, with at most 2 deaths per encounter
- Check: `dng.solo_clear`

#### Scenario: A clear within the performance budget
- **GIVEN** the population profile (the world at its default size) and the probe with a `/mg party` party
- **WHEN** the party clears The Deadmines with the run driver
- **THEN** the samples, taken every 10 s during the run, meet every limit of the performance-budget requirement "Server-side budget under the population profile":
  - process memory as max(resident, footprint): worldserver ≤ 6.0 GB, mysqld ≤ 1.0 GB, all three servers ≤ 7.0 GB;
  - world-update time p95 ≤ 100 ms and median ≤ 50 ms.
  - The evidence holds the samples
- Check: `dng.run_budget`

### Requirement: Zach plays two dungeons (M4 human gate)
Zach SHALL play two of the listed dungeons to the end with the M4 changes in place, as the M4 human gate (KICKOFF §8).

#### Scenario: Two dungeons, played for real
- **GIVEN** the stack with all three M4 changes merged, and Zach's character at about a listed dungeon's level (an alt, or a level set for the test)
- **WHEN** Zach plays two listed dungeons from the entrance to the final boss: one with a `/mg party` party, one solo or with companions as he likes
- **THEN** he finishes both without help from GM commands, and he signs the gate (or lists what blocks it)
- [human-verify] This is the milestone's human gate by definition (KICKOFF §8). It covers what the checks skip: walking through trash, doors and events, and the feel of the fights.
