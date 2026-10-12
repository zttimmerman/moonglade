# dungeon-companions Specification

## Purpose
TBD - created by archiving change m4-party-preset. Update Purpose after archive.

## Requirements

### Requirement: A dungeon party in one command
The Moonglade addon SHALL provide `/mg party`. It fills the player's party to five with companions covering the roles the player's own talent spec doesn't (by default a tank, a healer and two damage dealers), and `/mg party clear` removes them. Each companion SHALL join within 3 levels of the player, in its role's talent spec, auto-geared, with its role's strategies. The slash command SHALL send only stock mod-playerbots commands, taken from one table in `MoongladeParty.lua`.

#### Scenario: Party preset fills the roles
- **GIVEN** the probe character (a damage-dealing spec) at a listed dungeon's target level, with no party
- **WHEN** the probe sends, in order, the command list parsed from `MoongladeParty.lua` (the same table the slash command uses)
- **THEN** within 90 s the probe's party lists 4 online companions. Exactly one has the `tank` combat strategy and one has `heal` or `holy heal` (read by whispering `co ?` to each and parsing the reply). The two others have `dps assist`, `threat` and `wait for attack`. Every companion's level is within 3 of the probe's, and every one has all its armour slots filled except shirt and tabard (read from `character_inventory`)
- Check: `dng.party_preset`

#### Scenario: Role-aware fill
- **GIVEN** the probe character in a protection (tank) spec
- **WHEN** it sends the party command list
- **THEN** no companion has the `tank` strategy, one has a healing strategy, and three are damage dealers
- Check: `dng.party_preset_tank_player`

#### Scenario: The slash command sends the plan
- **GIVEN** the addon loaded under the Lua stubs, a player of each role (damage dealer, tank, healer)
- **WHEN** `/mg party` and then `/mg party clear` are run
- **THEN** the commands sent are exactly the plan's for that role, in order (addclass, then each companion's spec, autogear and strategy whispers), and `clear` sends the stock removal command; an unknown role sends nothing and says why
- Check: `unit:lua:test_addon_party.damage_dealer_gets_tank_healer_mage_rogue`
- Check: `unit:lua:test_addon_party.tank_player_gets_a_hunter_instead_of_a_tank`
- Check: `unit:lua:test_addon_party.healer_player_gets_a_tank_and_a_hunter`
- Check: `unit:lua:test_addon_party.steps_wait_for_the_companions_answer`
- Check: `unit:lua:test_addon_party.clear_sends_the_stock_removal`
- Check: `unit:lua:test_addon_party.unknown_role_sends_nothing_and_says_why`
- [human-verify] The real client's party frame filling is seen by Zach at the M4 gate. Reason: the command plan is unit-tested, the server's response is checked by `dng.party_preset`, and a real-client GUI run would cost minutes on every PR for no extra coverage.

### Requirement: AutoBalance's chat lines stay hidden
The player SHALL NOT see AutoBalance's "[AutoBalance] Combat has ended" system message (`ABPlayerScript.cpp:224`), which fires when a party member leaves mid-fight. mod-moonglade filters it; scaling itself is unchanged.

#### Scenario: The filter drops only AutoBalance lines
- **GIVEN** the filter's logic (pure function over a message)
- **WHEN** it sees the AutoBalance combat-ended line, another AutoBalance line, and an ordinary system message
- **THEN** both AutoBalance lines are dropped and the ordinary message passes
- Check: `unit:cpp:chat_filter.drops_combat_ended_line`
- Check: `unit:cpp:chat_filter.drops_other_autobalance_lines`
- Check: `unit:cpp:chat_filter.ordinary_messages_pass`

#### Scenario: Login text stays clean
- **GIVEN** the login text word list
- **WHEN** `client.login_text_clean` runs
- **THEN** "AutoBalance" is one of the words it fails on
- Check: `client.login_text_clean`
