## Why
Slice 1 of M4 companions, split out of `m4-dungeon-companions` so it ships in a day (Zach, 2026-10-11: changes ship within about a day; most tests are unit tests with mocks). It gives the player the main thing: a full dungeon party with one command.

## What changes
- `client-mods/addons/Moonglade/MoongladeParty.lua`: `/mg party` and `/mg party clear`, driven by one plan table (design and Zach's answers in `m4-dungeon-companions/proposal.md`: warrior tank, priest healer, mage, rogue; a hunter with a passive pet replaces the player's own role; damage dealers wait for the tank).
- `harness/hslib/config.py`: `AutoAvoidAoe = 1` and `ApplyInstanceStrategies = 1` set explicitly (they are mod-playerbots defaults at our pin; this keeps an upstream default change from turning them off).
- mod-moonglade: hides AutoBalance's "Combat has ended" chat line; `client.login_text_clean` also fails on "AutoBalance".
- `docs/companions-in-dungeons.md`: a one-page cheat sheet of the party command and orders.

## Tests
- Unit tests: Lua (the slash command's plan per role) and C++ (the chat filter), with stubs.
- One integration check, `dng.party_preset`: the server really adds the companions in the right roles (both role variants in one server session).

## Moved out of `m4-dungeon-companions`
The requirement "A dungeon party in one command" now lives here. Its real-client scenario becomes a unit test plus a human-verify at the M4 gate, for the reason given in the spec. Instance tactics, the behaviour checks and full dungeon runs stay in `m4-dungeon-companions` as later slices.

## Spec-change log
- 2026-10-11: the real-client scenario (`client.party_slash`) is replaced by `unit:lua:test_addon_party` plus a human-verify at the M4 gate. Recorded for the next human gate.
