## Why
Slice 2 of M4 companions, split out of `m4-dungeon-companions` so it ships in a day. Companions should use mod-playerbots' boss tactics in the dungeons that have them, which `ApplyInstanceStrategies = 1` (set explicitly by `m4-party-preset`) turns on.

## What changes
- One integration check, `dng.instance_strategy`: a `/mg party` party (the plan from the add-on) enters Utgarde Keep, then Hellfire Ramparts, then The Deadmines, and each companion's `co ?` is read inside each dungeon.
- Unit tests for any new pure logic (for example, parsing the strategy names expected per map).
- No product code is expected. If the strategies don't switch, that's a finding: the change reports it before any fix.

## Moved out of `m4-dungeon-companions`
The requirement "Dungeon tactics switch on by themselves" now lives here.
