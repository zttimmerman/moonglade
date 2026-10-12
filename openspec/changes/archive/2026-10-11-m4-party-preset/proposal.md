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

## As built (builder, 2026-10-11)
- **Whispers go in steps.** A companion handles all commands that arrived since its last update together, in no fixed order (`PlayerbotAI::HandleCommands`). Both `reset botAI` and `talents spec` reset its strategies. So `MG_PARTY_PLAN` gives each companion three steps: `reset botAI`, then `talents spec <spec>` once it answers "AI was reset to defaults", then `autogear` plus the role's extras once it answers "Picking <spec>". Sent all at once (the first build), the damage dealers lost `threat` and `wait for attack`. The add-on stops and says so if a companion doesn't join or answer within 30 s.
- **The player's role** comes from the talent tree with the most points, mapped like mod-playerbots' `IsTank`/`IsHeal`. No points yet means a damage dealer. "Unknown role" means the client hasn't loaded talents or class yet. A non-empty party or a raid is refused.
- **`/mg party clear`** sends `.playerbots bot remove *` (every companion in the party).
- **`dng.party_preset` runs at Hellfire Ramparts' target level (63).** mod-playerbots' autogear skips trinkets below level 50, neck and head below 30, and rings below 20 (`PlayerbotFactory.cpp:2361-2371`). At The Deadmines' level 19, the head slot stays empty by design. At 63, every armour slot is filled (all equipment slots but shirt, tabard and the weapon slots).
- **GIVEN, the probe's role:** one talent point in the arms or protection tree, chosen by a DBC rule and learned with the client's `CMSG_LEARN_TALENT`, then confirmed in `character_talent`.
- **The hunter answers `co ?` late** (up to ~13 s after its last step). The check asks every companion at once and asks again every 2.5 s, for up to 25 s. One server session runs both variants in about 50 s.
- **Open for the orchestrator:** the scenarios' unit Check ids (`unit:lua:test_addon_party`, `unit:cpp:chat_filter`) aren't full test ids, so `report.coverage` fails. It needs `unit:<runner>:<file or suite>.<name>`; see the builder report.

## Spec-change log
- 2026-10-11: the real-client scenario (`client.party_slash`) is replaced by `unit:lua:test_addon_party` plus a human-verify at the M4 gate. Recorded for the next human gate.
- 2026-10-11: the unit Check lines name each test id (the coverage rule wants full ids). A clarification.
