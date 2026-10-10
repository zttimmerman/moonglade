# Design: m4-dungeon-companions

Source pins: mod-playerbots `7bae1b5c` and core `7f12e89e` (fork.lock). Paths `vendor/mod-playerbots/src/...` are shortened to `pb/...`. This change builds on `m4-dungeon-scaling`: its driver `harness/hslib/dungeon.py`, its listed-dungeon table and rules (its design.md §6), and its prototype `docs/research/m4-dungeon-spike.md`.

## 1. What mod-playerbots already does (source survey, 2026-10-10)

### 1.1 Roles come from the talent spec
`pb/Bot/Factory/AiFactory.cpp`:
- `AddDefaultCombatStrategies` (:283-470) gives each class and spec tab its kit:
  - protection warrior (:323-324), protection paladin (:341-342), blood DK (:392-393) and feral bear (:359-360) get `tank`, `tank assist`, `pull`, `pull back`;
  - healing specs get `heal`/`holy heal` (:297-301 priest, :343-344 paladin, :353-354 druid);
  - everyone else gets `dps assist` (and `aoe`).
- Then:
  - `tank face` for tanks (:401-402);
  - `behind` for melee damage dealers (:404-405);
  - `save mana` and `healer dps` for healers (:407-413; `healer dps` is removed on maps listed in `AiPlayerbot.RestrictedHealerDPSMaps`, which includes the four listed dungeons' maps: conf:1389-1393);
  - `avoid aoe` for everyone when `AutoAvoidAoe` is on and the master is a real player or selfbot (:290-291; `HasGameClientMaster`, `pb/Bot/PlayerbotAI.cpp:4475`).
- The role tests are `PlayerbotAI::IsTank`/`IsHeal` (`PlayerbotAI.cpp:2287`, `:2325`), by spec tab.
- Spec switching: the whisper `talents spec <name>` (`pb/Ai/Base/Actions/ChangeTalentsAction.cpp:101-102`), with names from `AiPlayerbot.PremadeSpecName.<class>.<n>` (conf:1786-2011, e.g. `1.2 = prot pve`, `5.1 = holy pve`).

### 1.2 Dungeon tactics
- `PlayerbotAI::ApplyInstanceStrategies(mapId)` (`PlayerbotAI.cpp:1636-1770`) adds one strategy per map on entry when `AiPlayerbot.ApplyInstanceStrategies = 1` (conf:467-468), and clears all of them first.
- 5-player coverage (`pb/Ai/Dungeon/DungeonStrategyContext.h`):
  - TBC: AC, Seth, Mech, UB, MgT, Ramp;
  - WotLK: UK, Nex, AN, OK, DTK, VH, GD, HoS, HoL, Occ, UP, CoS, ToC, PoS, FoS.
- **No vanilla dungeon** has tactics (the "// Vanilla // ..." placeholder at `DungeonStrategyContext.h:39-40`). Two listed dungeons are covered: Hellfire Ramparts (map 543 → `tbc-ramp`) and Utgarde Keep (574 → `wotlk-uk`).

### 1.3 Orders and other behaviour
- **Orders:** chat triggers in `pb/Ai/Base/ChatTriggerContext.h`:
  - `attack`, `tank attack`, `pull`, `pull back`, `pull rti`;
  - `rti`, `focus heal`, `follow`, `stay`, `flee`, `summon`, `ready`, `wait for attack time`, `max dps`, `save mana`, `position`, `formation`, `reset botAI`;
  - `co`/`nc` to add or remove strategies.
- **Raid icons:**
  - `rti <icon>` sets the icon a bot attacks;
  - `rti cc <icon>` sets its crowd-control icon (`pb/Ai/Base/Actions/RtiAction.cpp:18-33`; values `RtiTargetValue.h:41-42`).
- **Crowd control in 5-player dungeons:**
  - the generic `cc` trigger fires **only on the `rti cc` target** inside a non-raid dungeon (`pb/Ai/Base/Trigger/GenericTriggers.cpp:575-586`), so bots don't sheep at random;
  - that's the context of upstream PR #2648 (merged to test-staging 2026-08-22);
  - `cc` is in the kits of mage, hunter, warlock and balance/cat druid (`AiFactory.cpp:320, 351, 358, 371, 389`).
- **Threat:** the `threat` strategy zeroes threat-generating actions at ≥80% threat on the current target, or ≥50% for AoE (`pb/Ai/Base/Strategy/ThreatStrategy.cpp:6-35`). It isn't in the default kits.
- **Waiting for the tank:** the `wait for attack` strategy holds non-pull actions for `wait for attack time` seconds (default 10, `pb/Ai/Base/Value/WaitForAttackTimeValue.h:24`) after combat starts. It applies to any bot that has it, tanks included (`WaitForAttackStrategy.cpp:29-56`), so it goes on damage dealers only.
- **Avoiding ground effects:** `AvoidAoeAction` (`pb/Ai/Base/Actions/MovementActions.cpp` ~:1840-2030) flees dynamic-object auras whose radius is ≤ `MaxAoeAvoidRadius` (conf:475, 15.0), unless whitelisted (conf:478). It also flees damaging periodic-trigger auras on units.

### 1.4 Upstream search (2026-10-10; `gh search prs|issues --repo mod-playerbots/mod-playerbots`)
- **Already in our pin:**
  - #2776 Hellfire Ramparts strategy (merged 2026-09-18);
  - #2803 dungeon clean-ups (2026-09-19);
  - #2702 Auchindoun edits;
  - #2663 MgT; #2604 Underbog; #2634 Sethekk;
  - #2648 dungeon cc gating.
- **Newer than our pin** (mod-playerbots master is 60 ahead): no new 5-player tactics. #2836 renamed class strategies (it would change strategy names our preset sends, so read it before any bump). #2845 fixed druid tree-form spam. #2912 (open) extends target exclusions to RTI and AoE.
- **Not found:** no classic-dungeon tactics PR, and no issues about AutoBalance or SoloCraft interplay.
- **Decision:** no pin bump for this change. If task 1.1 shows a classic-dungeon failure, follow the hs-change-loop "check upstream first" order before writing `patches/playerbots/0002`.

## 2. Party preset (`/mg party`)
- **`client-mods/addons/Moonglade/MoongladeParty.lua`** holds one table, `MG_PARTY_PLAN`. The harness parses it with a small Lua-table reader, so the check and the addon can't drift.
  - Per role: class word for `addclass`, premade spec name, extra `co` strategies, and an optional `wait for attack time`.
  - Default (Zach question 1): tank = warrior `prot pve`; healer = priest `holy pve`; dps = mage `frost pve` + rogue `combat pve`; a third dps when the player is a tank or healer = hunter, its pet stance set through the playerbots `pet` command so it doesn't pull.
  - The spec names are read from `playerbots.conf.dist` at build time inside `dng.party_preset`, so a renamed spec (upstream #2836) fails loudly.
- **Flow:**
  1. Detect the player's own role from their active talent tree, using `GetTalentTabInfo` points: the tank or healer tree by class.
  2. For each missing role, `.playerbots bot addclass <class>`.
  3. Wait for the `SMSG_GROUP_LIST` member (the addon listens for `PARTY_MEMBERS_CHANGED`).
  4. Whisper, in order: `reset botAI` (F16: companions keep old orders across logins), `talents spec <name>`, `autogear`, then the role's `co` line (damage dealers: `co +threat,+wait for attack` and `wait for attack time 3`).
  - The `wait for attack time` value is a tuning knob set by task 3.3, and the run that justified it is recorded.
- **`/mg party clear`:** `.playerbots bot remove <name>` for each companion (ADR-0008's patch takes them out of the party).
- **No server code**, and permissions are stock:
  - `AiPlayerbot.AddClassCommand = 1` lets a non-GM player use `addclass` (`PlayerbotMgr.cpp:1083-1089`, conf:140);
  - the master's own bots accept all commands.
- **Config** (`harness/hslib/config.py`, `playerbots`): `AiPlayerbot.AutoAvoidAoe = 1` and `AiPlayerbot.ApplyInstanceStrategies = 1` set explicitly with a comment. They're the current defaults, pinned so an upstream default change can't silently drop them.

## 3. Tactics checks: how they observe
- **Probe additions** (`harness/hslib/probe/client.py`):
  - parse `SMSG_SPELLHEALLOG` (caster, target, spell, amount, overheal);
  - send and parse `MSG_RAID_TARGET_UPDATE` (set icon: u8 index + u64 guid);
  - parse `co ?` whisper replies (strategy list).
  - Formats follow the core's packet builders. The builder cites the file:line of each one (`Unit::SendHealSpellLog`, `Group::SetTargetIcon`).
- **`dng.tank_holds`:**
  - victims of the boss's `SMSG_ATTACKERSTATEUPDATE` swings, counted from 5 s after the first swing (the opener, before the tank's threat builds, is excluded on purpose);
  - threshold: ≥80% on the tank. Reason: tank-swap and knockback mechanics exist, and the first UK boss (Prince Keleseth) has adds. The raw counts are in the evidence;
  - the counterexample removes the tank from the party.
- **`dng.healer_heals`:** heal events from the healer, plus party deaths from `SMSG_ATTACKSTOP` with `now_dead` and the group list's member flags.
- **`dng.avoid_aoe`:** the spell is chosen by a Spell.dbc rule.
  - Effect `SPELL_EFFECT_PERSISTENT_AREA_AURA` (27, `SharedDefines.h`) with aura `SPELL_AURA_PERIODIC_DAMAGE` (3).
  - Radius from SpellRadius.dbc ≤ `MaxAoeAvoidRadius`; duration from SpellDuration.dbc ≥ 10 s; not whitelisted; lowest ID.
  - The GM `.cast target <id>` makes the selected creature cast at its victim (core `cs_cast.cpp`).
  - Positions are read with `.gps` on the companion (selected by the probe) every 0.5 s.
- **`dng.instance_strategy`:**
  - whisper `co ?` inside each dungeon;
  - the expected names come from parsing `ApplyInstanceStrategies`' `case <map>: strategyName = "<x>"` table in the pinned source, not from memory.
- **`dng.rti_focus` and `dng.cc_on_mark`:**
  - pack and humanoid chosen by SQL rule (spec text);
  - the polymorph aura is identified by Spell.dbc rule (aura `SPELL_AURA_TRANSFORM`, spell in the mage's spell list from `character_spell`);
  - damage events on the marked creature are read from the probe's combat-log events.

## 4. The run driver (`dungeon.run(...)`)
- **Order:** required encounters (the rule from `m4-dungeon-scaling` design.md §6) sorted by DungeonEncounter.dbc `orderIndex`.
- **Staging point for an encounter:**
  - the boss spawn's position, moved 30 yd back toward the previous staging point (the dungeon entrance for the first) along the straight line;
  - then snapped to the navmesh with the core's own path query, via the GM command `.mmap path`, from the previous staging point;
  - if the snapped point is more than 10 yd off the line, use the last navmesh point on the path that is ≥ 25 yd from the boss.
  - This is a rule, not hand-picked coordinates. Task 1.1 confirms `.mmap path` output can be parsed headless, and records a fallback if not.
- **Per encounter:**
  1. Teleport the probe there (`.go xyz`).
  2. Whisper `summon` to the companions.
  3. Wait until everyone is within 15 yd, out of combat, and the healer's mana is ≥ 80% (or 60 s).
  4. Select the boss (or the nearest hostile in the boss's pack). Whisper `tank attack` to the tank, or for the solo run let the selfbot attack. Poll until the boss is dead (its encounter bit is set) or all party members are dead.
  - **On a wipe:** `.revive` each member, teleport back to staging, wait for the boss to reset (evade), and retry. Max 2 wipes, then FAIL with evidence.
- **GM permissions:** `.mmap path`, `.cast target`, `.gps`, `.go xyz`, `.die` and `.revive` need RBAC permissions. Task 1.1 lists which of them the companion account's security level 2 lacks. If some are missing, the dungeon checks use a separate harness account at level 3, the same precedent as HSSAVES in front-menu-saves, and never PLAYER.
- **Honest scope:** the driver teleports between bosses. It doesn't walk the trash, open doors or run events, though trash near a boss gets pulled with it. An event-summoned encounter (the Razorfen Downs gong) is reported as "skipped: no static spawn" and isn't required. The walk-through is the human gate.
- **Solo run:**
  - `.playerbots bot self` (GM, `SelfBotLevel = 1`) makes the probe's character AI-driven;
  - "self" AI on the probe is task 1.1's riskiest unknown;
  - if it fails, `dng.solo_clear` becomes `[human-verify]` with that reason, through a spec-change log entry (a loosening, reviewed at the gate).

## 5. Tiers and verification budget
| Check | Tier | Approx. time |
|---|---|---|
| `dng.party_preset`, `dng.party_preset_tank_player` | full | ~2 min each |
| `dng.tank_holds`, `dng.healer_heals` (one shared UK fight, two records) | full | ~4 min |
| `dng.avoid_aoe`, `dng.instance_strategy`, `dng.rti_focus`, `dng.cc_on_mark` | full | ~2–4 min each |
| `client.party_slash` | gui (path rule: `client-mods/addons`) | — |
| `dng.party_clear` (4 dungeons), `dng.solo_clear`, `dng.run_budget` (population profile) | slow | ~60–90 min total |

- **Verifier:** one full run, plus the new full-tier checks 3× each, plus the slow checks once each. The slow checks' 3× repeats move to the nightly sweep (hs-change-loop flake rule). This is a recorded test-method decision, not escalated (dont-escalate-test-internals). It keeps the verifier inside ~2 h. That's above the 90-minute target because of the long runs, and the orchestrator should split the slow checks into a second verifier session if needed.
- **Lane B:** the companion checks need the populated companion template in lane B (second-stack follow-up), so they stay in lane A for now.

## 6. Interactions
- **Scaling (`m4-dungeon-scaling`):** every run asserts `.ab mapstat` player count = party size (1 or 5), so a run never passes on unscaled or wrongly scaled creatures.
- **True pause:** companions freeze with the world (`pause.bots_frozen`). There's no new interaction.
- **Saves:** an autosave on arriving in a dungeon (front-menu-saves, "arriving on a new map") holds the player only, and companions aren't saved (bot sessions never save).
- **F16:** the preset sends `reset botAI` first, so a companion's leftover orders from an earlier session can't break a run.

## 7. If classic dungeons need tactics
A failing `dng.party_clear` on The Deadmines or Razorfen Downs is diagnosed from the run evidence: which boss, which role died, and how. The fix order:
1. a config or strategy tweak in the preset;
2. upstream search and a pin bump or cherry-pick;
3. our own playerbots patch with a small classic-dungeon strategy (e.g. kill VanCleef's adds first), each with its own task and estimate.

It is never a scaling change to hide a tactics problem, unless Zach's difficulty answer asks for easier dungeons.
