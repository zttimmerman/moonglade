# Design: m4-dungeon-scaling

Pins read for this design: core `7f12e89e` (fork.lock `core_sha`, 2026-09-20) and mod-playerbots `7bae1b5c` (`playerbots_sha`, 2026-09-20). Paths starting `vendor/` are relative to the repo root once `hs build` has synced the vendor trees. Module sources were read from GitHub tarballs of the commits named below. Research date: 2026-10-10.

## 1. Module choice

### 1.1 Candidates
| | mod-autobalance | mod-solocraft | Write our own |
|---|---|---|---|
| Repo | https://github.com/azerothcore/mod-autobalance | https://github.com/azerothcore/mod-solocraft | — |
| Commit read | `1d707cb61a0962ab01e4123681e2d3b66cc558a2` (master, 2026-10-07, "fix(CORE): Don't inherit level from friendly summoners (#228)") | `59588838988d1b74111d26670d99d0e649014b93` (master, 2026-09-21) | — |
| Licence | No LICENSE file. `src/AutoBalance.cpp` header: GPL v2 "or (at your option) any later version". Other headers point to AzerothCore's licence. | GPL-3.0 (LICENSE file) | ours |
| Activity | 165 stars, 37 open issues, last push 2026-10-07 | 75 stars, 4 open issues, last push 2026-09-21 | — |
| What it changes | **The monsters.** Health, mana, armour, damage and CC duration of instance creatures, from the number of players in the map (`ABUtils.cpp` `UpdateMapPlayerStats` :2425-2540; multiplier formula :641-670, a tanh curve over player count with an inflection point, floor and ceiling). Optional creature level scaling and XP/money scaling. Per-dungeon and per-boss overrides. `.ab mapstat` and `.ab creaturestat` show the live numbers. | **The players.** On entering a dungeon, each player gets stat and spell-power buffs worth (dungeon difficulty / number in group) (`Solocraft.cpp` `ApplyBuffs` :679-850). Values are stored in a custom characters-DB table `custom_solocraft_character_stats` (:658, :696). Members who join after entry get a "debuff" until everyone re-enters (conf `SoloCraft.Debuff.Enable`). | — |

### 1.2 Decision: mod-autobalance
- **It scales the right side.** Companions are real `Player` objects, and AutoBalance counts every non-GM player in the map (`ABUtils.cpp` `AddPlayerToMap` :2620-2670 skips only `IsGameMaster()`). One player plus four companions is therefore "5 players", which is the unchanged dungeon, and the solo player alone is "1". That is exactly M4's "1 player plus N bots". It re-evaluates when players join or leave (with a combat lock, :2470-2530), so summoning a companion mid-dungeon just works.
- **SoloCraft fits worse:**
  - Its buffs are divided among group members at the moment you enter, and companions summoned later trigger the "overloading" debuff. Our usual flow is to enter and then summon.
  - It buffs the bots too.
  - It writes per-character rows to a custom characters-DB table. That's a new table the character-saves spec would have to classify ("in the save, kept, or removed"). AutoBalance keeps no character data.
  - Its recent history is buff-accounting fixes (#63, #67, #69).
- **Writing our own** would repeat AutoBalance's per-creature bookkeeping (summons, clones, vehicles, level scaling) for no gain. CLAUDE.md §2 asks to evaluate before writing, and this is the evaluation.
- **Pin:** master at `1d707cb6`. The `stable` tag (`5d2778e3`, 2024-09-10) predates the core API renames that our pin has. AutoBalance #213 renamed `Creature_SelectLevel` → `OnCreatureSelectLevel`, and #217 (2025-11-28) adapted to AzerothCore PR 23346. Our core has `OnCreatureSelectLevel` (`vendor/azerothcore-wotlk/src/server/game/Scripting/ScriptDefines/AllCreatureScript.h:36`) and `Unit::SetStatFlatModifier` (`Unit.h:1084`), so `stable` would not compile and master should. Confirming that is prototype task 1.1.

### 1.3 Licence and release
GPL-2.0-or-later code, compatible with our GPL-2.0-or-later. The release's THIRD_PARTY entry must quote the `src/AutoBalance.cpp` header, because there is no LICENSE file to copy, and it must name AzerothCore's licence for the files whose headers point there. Recorded in ADR-0014. The release source manifest gets the module tree, like mod-playerbots (`tools/release/release.py` :363, :456).

### 1.4 Upstream search (2026-10-10)
Run with `gh search issues|prs` on `azerothcore/mod-autobalance`, `mod-playerbots/mod-playerbots` and `mod-playerbots/azerothcore-wotlk`.

- **Build at our pin:**
  - #220 "No member named 'SetStatFlatModifier'" (open). This happens on older cores. Ours has the member.
  - #214 "OnCreatureSelectLevel did not override" (open). This is an old core with a new module. Ours matches.
- **Crash risk:**
  - #222 (open, 2026-05) is an access violation in `_isSummonCloneOfSummoner` during `Creature::Respawn` of a transport NPC (map 610), reached via `.respawn`. No listed dungeon has transports. Watch for it in the full run.
  - #148 (closed) was NPCBots, not playerbots.
- **Scaling oddities to watch:**
  - #198 (open) over-downscaling when under-levelled (Stockades). This is relevant to level scaling, which is off by default here (§2).
  - #201 (open) too much XP when scaled up. We don't scale up by default.
  - #215 (open) sporadic rewards with `Enable.Global` off. We use the module on.
  - #224 (open) Dire Maul West Immol'thar. That's not a listed dungeon.
- **Playerbots interplay:** no AutoBalance issues in mod-playerbots or its core fork (searches "autobalance" and "solocraft" returned unrelated hits). Two AutoBalance issues mention NPCBots (#208, #148), which is a different bot system.
- **Pins vs heads:**
  - mod-playerbots master is 60 commits ahead of `7bae1b5c`. None of them are 5-player dungeon tactics. The relevant newer items are #2912 (open PR, "Extend target exclusions to RTI and AoE") and #2836 (renamed class strategies). `m4-dungeon-companions` decides whether to bump.
  - The core fork is 237 commits ahead. There's no need to bump for this change.

## 2. Scaling approach
- **Section `autobalance`** in `harness/hslib/config.py`. It's rendered into `server/etc/modules/AutoBalance.conf`, from `AutoBalance.conf.dist`, which the module's CMake installs. It's included in `MODULE_SECTIONS` and in lane B's AC_* union (`config.module_env`), like `era` and `moonglade`. Keys Moonglade sets (anything not listed keeps the module default), each with its reason:
  - `AutoBalance.MinPlayers = 1` (5-player normal and heroic). This is the module default, stated explicitly: solo scales to one.
  - `AutoBalance.InflectionPoint`, `.CurveFloor`, `.CurveCeiling`, `.BossModifier`, and the same four for `InflectionPointHeroic`. Starting values are the module defaults (0.5 / 0.0 / 1.0 / 1.0). Task 3.3 tunes them from `scale.solo_final_boss` runs and records each run in `docs/research/m4-scaling-tuning.md`. The formula's `curveCeilingAdjustment` (`ABUtils.cpp` :655-660) normalises the curve so that the dungeon's maximum player count gives exactly `CurveCeiling`. With ceiling 1.0, five players get the unchanged dungeon, which is what `scale.party_stock` asserts.
  - `AutoBalance.playerCountDifficultyOffset = 0`. Zach's question 2: if he picks "companions count as half a person", this goes negative or a per-bot weight is needed. AutoBalance has no per-bot weight, so that answer would need a small module patch. It's flagged in the proposal, and the default needs none.
  - `AutoBalance.LevelScaling = 0` (question 3, default "dungeons keep their own levels"). This also sidesteps #198.
  - `AutoBalance.RewardScaling.XP = 1`, `.Money = 1`, `.Method = "dynamic"` (question 4, default "less XP and gold when weaker"). These are the module defaults, stated explicitly.
  - `AutoBalance.Enable.OtherNormal/OtherHeroic` stay at the defaults (1). Raids are scaled too, as a side effect, which is harmless and out of M4's scope.
  - `AutoBalance.PlayerChangeNotify`: the module default (1) announces scaling changes in chat. **Default to 0**: the seamless-experience rule says no server chatter. `.ab mapstat` stays available.
- **Profiles:**
  - `scaling-off` = m0 + `AutoBalance.Enable.Global = 0`, for `scale.disabled_stock` and the unscaled reference reads.
  - `lockouts-stock` = m0 + `AccountInstancesPerHour = 5` + `Moonglade.Dungeons.FreshAfterClear = 0`.
- **No game data values** are written. The knobs are module tuning parameters, not WoW data (§7 of CLAUDE.md). Each tuned value records the run that justified it.

## 3. Lockouts
- **Hourly cap:**
  - The core refuses a non-raid dungeon entry when `_instanceResetTimes.size() >= AccountInstancesPerHour` (`MapMgr.cpp:232-244`, `PlayerStorage.cpp:7187-7192`), and 0 would block everything.
  - Set `AccountInstancesPerHour = 1000` in the worldserver overrides. Reason: comfortably above any solo pace (one entry every 3.6 s for an hour), and still a finite guard against a runaway script. `lock.no_hourly_cap` checks it.
- **Fresh after clear** (`modules/mod-moonglade/src/MoongladeDungeons.cpp`, PlayerScript):
  - **Hook:** `OnPlayerCanEnterMap(player, entry, instance, mapDiff, loginCheck)` runs at `MapMgr.cpp:165`. That's inside `PlayerCannotEnter`, before the destination instance is picked (`MapMgr.cpp:225`, `MapInstanced.cpp:141` `PlayerGetDestinationInstanceId`). It always returns true; it never blocks entry.
  - **Skip** when `loginCheck` is set, when the map isn't a non-raid dungeon, when the player is a bot (the playerbots core fork's `WorldSession::IsBot()`, `vendor/azerothcore-wotlk/src/server/game/Server/WorldSession.h:1231`; task 2.3 confirms that addclass and alt bots set it), or when the key is 0.
  - **Find the save:** `sInstanceSaveMgr->PlayerGetInstanceSave(guid, mapId, difficulty)`.
  - **When it's finished:** its completed-encounter mask (`InstanceSave::GetCompletedEncounterMask`, `InstanceSaveMgr.h:72`) covers every encounter of `sObjectMgr->GetDungeonEncounterList(mapId, difficulty)` whose `lastEncounterDungeon` ≠ 0 (`ObjectMgr.h:707-713, 953`), and the instance map (if loaded) holds no non-bot player:
    - reset it the way the core's own "Reset all instances" does (`PlayerMisc.cpp:191-230`): `InstanceMap::Reset(INSTANCE_RESET_ALL)` if loaded, then `DeleteInstanceSavedData` and `UnbindAllFor(save)`;
    - this unbinds the player and every companion bound to it (AzerothCore has per-player binds, so there's no group bind to clear).
  - **When bots are still inside the old copy** (Reset fails while players are in it): unbind only (`PlayerUnbindInstance` for the player and their group's bots), so the player gets a new copy. The bots follow the master or are summoned (playerbots `SummonWhenGroup`), and the old map unloads on `Instance.UnloadDelay`.
  - Normal and heroic 5-player dungeons are handled the same way. The heroic daily lock (a permanent bind) is cleared only after a full clear, so a half-done heroic stays.
- **How checks clear a dungeon quickly:**
  - The GM `.die` on a targeted boss calls `Unit::Kill` (`cs_misc.cpp` ~:1190), which runs `KillRewarder`, which credits the encounter (`KillRewarder.cpp:309-313`, `Map::UpdateEncounterState` → `SetCompletedEncountersMask(..., true)`, `Map.cpp:2965-3000`, `InstanceScript.cpp:756-771`).
  - `.instance setbossstate` does **not** set the mask, so checks don't use it.

## 4. Build and release integration
- **fork.lock:** `autobalance_repo=https://github.com/azerothcore/mod-autobalance.git`, `autobalance_branch=master`, `autobalance_sha=1d707cb6…`, plus a comment.
- **`build.py`:**
  - `sync_vendor` clones it to `vendor/mod-autobalance` (`_ensure_clone`), checks it out, cleans it, and adds it to `_expected_links()` and `BUILD_PINS`;
  - `pin_state` is enforced like playerbots;
  - a `patches/autobalance/` queue is used only if a patch becomes necessary (none planned).
- **The build-input manifest** gains the pin, so a bump forces a from-empty build.
- **Elsewhere:** `bootstrap.py` (pin verification list), `baseline` (same module set), `tools/release` (THIRD_PARTY, source manifest, the conf in the payload). `release_paths_golden.json` changes in a reviewed way.
- **Tests that must stay green:** `build.pin_enforced`, `build.clean_smart`, `release.*`, `public.*`. This is a third-party clone under `vendor/`, so nothing new lands in the public tree except fork.lock lines.

## 5. Test approach
- **Driver `harness/hslib/dungeon.py`.** It builds on `companion.py`: the probe on the harness's own companion account (P.COMP_USER, GM level 2), never PLAYER or HSTEST.
  - `enter(dungeon, difficulty)`: `.gm off` (AutoBalance ignores GM-mode players, and so do creatures), `.character level`, `initself=<quality>` for the probe's own gear (`PlayerbotMgr.cpp:922-980`, GM-only), then a GM teleport into the dungeon (`.go xyz <entrance> <map>` from `areatrigger_teleport` by rule, or `.go creature <spawn>`). It waits for `SMSG_NEW_WORLD` to the dungeon map.
  - `party(roles)`: `.playerbots bot addclass <class>` then a whisper `talents spec <premade spec name>` (spec names from `playerbots.conf` `AiPlayerbot.PremadeSpecName.<class>.<n>`) and `autogear`.
  - `boss_info(entry)`: max health via `.npc info` and `.ab creaturestat` on the selected boss.
  - `completed_mask()`: `characters.instance.completedEncounters` for the probe's bound instance.
  - `kill(guid)`: `.die`, lockout checks only.
- **Selfbot (task 1.1 decides):** `.playerbots bot self` (GM-only at `SelfBotLevel = 1`, conf:189-191; `PlayerbotMgr.cpp:1053-1072`) hands the probe's character to the playerbots AI so it fights like a companion. Unverified for a headless session: the server moves a selfbot with splines, and the probe has to send nothing back except the time-sync and teleport acks it already sends. **Fallback:** a passive probe master that selects the boss and whispers `attack` to the party. That works for `m4-dungeon-companions`, but solo runs would need the selfbot, so if that fails the solo scenario becomes `[human-verify]` with that reason, recorded in the spec-change log.
- **Checks:**

  | Check | Tier, notes |
  |---|---|
  | `scale.module_loaded`, `scale.release_notice` | quick / full |
  | `scale.solo_weaker`, `scale.party_stock`, `scale.counts_companions`, `scale.disabled_stock`, `scale.outdoor_untouched`, `scale.pause_safe` | full, ~1–2 min each |
  | `lock.*` | full, ~1–2 min each |
  | `scale.solo_final_boss` | **slow**: 4 dungeons, up to 10 min each |

- **Counterexamples:**
  - `scaling-off` makes `scale.solo_weaker` FAIL;
  - a probe left in GM mode makes the companion-count checks FAIL;
  - `lockouts-stock` makes `lock.no_hourly_cap` and `lock.heroic_fresh_after_clear` FAIL;
  - a sabotaged module build with the hook returning early makes `lock.fresh_after_clear` FAIL.
- **Lane B:** `scale.*` and `lock.*` (except slow) need only their own stack. They're candidates for `LANE_B` after their first green runs. The companion account in lane B needs a populated template (STATUS follow-up from second-stack), so they start in lane A.
- **Verification budget** (hs-change-loop): one full run, each new check 3×, and the slow `scale.solo_final_boss` once per dungeon. Its 3× repeat moves to the nightly sweep, because 3 × 4 × 10 min is beyond the 90-minute cycle. This is a test-method decision, recorded here and not escalated.

## 6. Dungeon facts by rule (CLAUDE.md §7)
`dungeon.py` resolves each listed dungeon from its LFG id and never hard-codes names, levels or entries.

| Fact | Rule and source |
|---|---|
| Levels and map | `LFGDungeons.dbc` row by ID: `MinLevel` [18], `MaxLevel` [19], `Target_Level` [20], `MapID` [23], `Difficulty` [24], `TypeID` [26] (=1 dungeon). Layout: `DBCStructure.h:1280-1300` `LFGDungeonEntry`, `DBCfmt.h:82`. |
| Encounters | `DungeonEncounter.dbc` rows for the map and difficulty: `mapId` [1], `difficulty` [2], `orderIndex` [3], `encounterIndex` [4] (the mask bit), name [5]. Layout: `DBCfmt.h:47`. Joined to world `instance_encounters` (entry = encounter ID) for `creditType`, `creditEntry` and `lastEncounterDungeon`. |
| Final boss | the `instance_encounters` row with `lastEncounterDungeon` = the LFG id. |
| Required encounters | Encounters whose `creditEntry` has at least one `creature` spawn on that map with `spawnMask` covering the difficulty. Encounters summoned only by events are reported but not required for "finished" in `m4-dungeon-companions` (the Razorfen Downs gong). |
| Boss position | That spawn's `creature.position_*`. |
| Entrance | `areatrigger_teleport` row whose `target_map` is the map. |

Values seen on 2026-10-10 (client-data v20.0 DBCs, core base SQL), for the proposal table only. Checks re-read them at runtime:
- Deadmines LFG 6: 15–25, target 19, map 36. Encounters 161–167, final 167 Edwin VanCleef (639).
- Razorfen Downs LFG 20: 32–42, target 36, map 129. Encounters 434–437, final 437 Amnennar the Coldbringer (7358).
- Hellfire Ramparts LFG 136: 57–67, target 63, map 543. Normal encounters 392/394/396, final 396 Vazruden the Herald (17537). Heroic LFG 188.
- Utgarde Keep LFG 202: 68–80, target 72, map 574. Normal 571/573/575, final 575 Ingvar the Plunderer (23954). Heroic LFG 242 (encounters 572/574/576).

## 7. Interactions
- **Vanilla mode (mod-era-profile):** its creature side table covers outdoor 1–60 creatures (`era_creature_template.sql` header: "outdoor 1-60 creatures", maps 530/571/609 excluded). AutoBalance touches instance creatures only (`Enable.Global` applies to dungeons and raids). `scale.outdoor_untouched` guards the boundary.
- **True pause (core patch 0003):** AutoBalance's per-map work runs in map update hooks that the pause starves. `scale.pause_safe` checks that nothing rescales across a pause. The combat lock is event-driven, not time-driven (`ABUtils.cpp` :2470-2530).
- **Character saves:** AutoBalance keeps no character rows. Instance binds are outside the save set by spec ("SHALL NOT change … instance lockouts"). Loading a save whose position is inside a dungeon puts the character back at that position. The core sends them to the dungeon's entrance if their bound copy no longer exists, which is stock behaviour.
- **Population profile:** scaling is per instance, so the 500-bot world is unaffected. Random bots that queue LFG (off in the population profile, `RandomBotJoinLfg = 0`) would also be scaled correctly.

## 8. Open items for builders (not Zach decisions)
- Bot detection inside mod-moonglade without linking playerbots: confirm that `WorldSession::IsBot()` is set for addclass and alt bots (task 2.3).
- Does `.go xyz` into an instance map respect the access requirement for a GM with `.gm off`? Prototype 1.1 records it. If it's refused, use the entrance area trigger (`.go trigger`).
