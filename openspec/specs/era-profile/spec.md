# era-profile Specification

## Purpose
"Vanilla mode": levels 1–60 use pre-2.3 pacing and difficulty taken from VMaNGOS data, switchable with one setting, leaving every TBC/WotLK system and all content above level 60 untouched.

## Requirements

### Requirement: One switch, on by default
The server SHALL read `EraProfile.Enable` (default 1). With 1, every signed vanilla change is in effect; with 0, the game behaves exactly like stock WotLK. Switching takes effect at the next server start and SHALL NOT alter any character's level, XP, items, or quest state.

#### Scenario: Default is vanilla mode
- **GIVEN** a freshly rendered config with no EraProfile override
- **WHEN** the stack starts
- **THEN** the module reports vanilla mode active in the worldserver log, and the vanilla spot checks below hold
- Check: `era.default_on`

#### Scenario: Toggle round-trip preserves characters
- **GIVEN** the test character at a known level, XP, and quest state with vanilla mode on
- **WHEN** the harness restarts the stack with `EraProfile.Enable = 0`, verifies WotLK values, then restarts with 1 again
- **THEN** each mode shows its own spot-check values, and the character's level, XP, inventory and quest status are identical before and after both switches
- Check: `era.toggle_roundtrip`

### Requirement: Vanilla XP curve
With vanilla mode on, XP required per level for levels 11–59 SHALL equal VMaNGOS's `player_xp_for_level` values; levels 1–10 and 60–79 SHALL equal AzerothCore's.

#### Scenario: XP-to-level spot checks
- **WHEN** the harness reads the XP required at levels 10, 20, 40, 59 and 60 from the running server (a GM query of a character at that level, or the server's own XP reporting)
- **THEN** levels 20, 40 and 59 match the VMaNGOS snapshot values and levels 10 and 60 match AzerothCore's, with every expected value read from the source DBs at check time
- Check: `era.xp_curve`

### Requirement: Vanilla quest XP
With vanilla mode on, the XP awarded for completing an in-scope quest at its own level SHALL equal the VMaNGOS-derived value (same over-level penalty rules as stock); out-of-scope and unmappable quests SHALL award stock XP.

#### Scenario: Quest XP spot checks
- **GIVEN** a test character at the quest's level
- **WHEN** the harness completes and rewards quest 10 (Scrimshank Redemption, L48) and quest 5944 (In Dreams, L60), and one needs-review quest from the era-diff report
- **THEN** the XP gained equals the VMaNGOS-derived value for 10 and 5944, and the stock value for the needs-review quest
- Check: `era.quest_xp`

### Requirement: Outdoor elites restored with elite HP
With vanilla mode on, the in-scope open-world creatures SHALL have VMaNGOS's rank and HealthModifier.

#### Scenario: Elite spot checks
- **WHEN** the harness spawns or inspects creatures 678 (Mosh'Ogg Mauler), 436 (Blackrock Shadowcaster) and 623 (Skeletal Miner) on the running server
- **THEN** each has elite rank and max HP equal to the VMaNGOS-derived value (base HP × VMaNGOS HealthModifier at that level), and an out-of-scope creature (any creature on map 571) is unchanged
- Check: `era.elites`

### Requirement: Vanilla mounts and riding
With vanilla mode on, vendor mount items in scope SHALL have VMaNGOS required levels and prices, and Apprentice/Journeyman riding SHALL have VMaNGOS required levels and costs.

#### Scenario: Mount and riding spot checks
- **WHEN** the harness queries items 2411 (Black Stallion Bridle) and 18766 (Swift Frostsaber) and riding spells 33388/33391 at their trainers on the running server
- **THEN** required levels and prices equal the VMaNGOS-derived values, and Expert/Artisan/Cold Weather riding are unchanged from stock
- Check: `era.mounts`

### Requirement: WotLK content untouched
Vanilla mode SHALL NOT change any entity outside the signed scope.

#### Scenario: Out-of-scope rows are byte-identical to stock
- **GIVEN** a baseline hash of every out-of-scope row in the touched tables (creature_template, quest_template, item_template, trainer spells, player_xp_for_level), taken with vanilla mode off
- **WHEN** vanilla mode is on
- **THEN** the hashes of all out-of-scope rows are identical, and the count of changed in-scope rows equals the era-diff report's change count
- Check: `era.scope_guard`

#### Scenario: Zach plays vanilla mode
- **GIVEN** vanilla mode on (default)
- **WHEN** Zach levels a new character for a session and visits an elite area
- **THEN** pacing feels slower and elites are a real threat, and nothing about Outland/Northrend/Death Knights is broken
- [human-verify] Feel of pacing and difficulty can only be judged by playing. Checked at the next human gate.
