## ADDED Requirements

### Requirement: Dungeon tactics switch on by themselves
In a dungeon that mod-playerbots has tactics for, each companion SHALL have that dungeon's instance strategy while it is inside, and SHALL lose it after leaving.

#### Scenario: Utgarde Keep and Hellfire Ramparts tactics
- **GIVEN** a `/mg party` companion party
- **WHEN** the party enters Utgarde Keep, then Hellfire Ramparts, then The Deadmines
- **THEN** inside Utgarde Keep every companion's `co ?` reply lists `wotlk-uk`; inside Ramparts every reply lists `tbc-ramp` and not `wotlk-uk`; inside the Deadmines neither is listed. The strategy names come from `PlayerbotAI::ApplyInstanceStrategies`'s map table, keyed by the map ids from LFGDungeons.dbc
- Check: `dng.instance_strategy`

