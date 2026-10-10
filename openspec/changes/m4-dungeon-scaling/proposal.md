## Why
M4 ("Solo tuning", KICKOFF §8) asks for dungeon scaling for 1 player plus N companions, tuned so a listed set of dungeons can be finished. Zach's wishes (STATUS, 2026-10-04 to 10-07): dungeons he can finish alone or with AI companions, difficulty that adapts when he's alone, and relaxed lockouts so he can rerun a dungeon whenever he likes.

M4 is split into three changes, built in this order:

1. **`m4-dungeon-scaling` (this change):** how hard a dungeon is for the party in it, and when it resets. It's mostly a third-party module plus config, with a small mod-moonglade part. It holds the riskiest prototype (task 1.1), which the other two changes build on.
2. **`m4-dungeon-companions`:** companion tactics in dungeons (tank, healer, staying out of bad spots), a one-command dungeon party, and the checks that the listed dungeons can be finished. It ends at the M4 human gate.
3. **`m4-alt-companions`:** your own other characters as companions (the "companion roster").

Why split: scaling and lockouts are server config and module work that can be verified in about an hour. Companion tactics and full dungeon runs are long, flaky-by-nature bot behaviour with slow-tier checks. Keeping them apart keeps each verifier cycle under the 90-minute budget, and a FAIL in one doesn't block the other. Alt companions are independent of both and small.

## What Zach gets (plain language)
- **Dungeons fit the size of your party.** Walk into a dungeon alone and its monsters and bosses have less health and hit softer. Bring companions and the dungeon gets tougher with each one, until a full party of five meets the normal, unchanged dungeon. This is automatic and adjusts as people join or leave (outside a fight).
- **Rerun dungeons freely.** There's no "you have entered too many instances" limit any more. When you finish a dungeon (its last boss is dead) and walk out, the next time you go in it's fresh. If you leave halfway, for example after dying, your progress is still there when you come back. Heroic dungeons work the same way, with no once-a-day lock.
- **One switch for each.** Scaling and the relaxed lockouts can each be turned off in the server config, which gives the normal WotLK behaviour back. They become Settings options later, with the planned difficulty presets.

## What Changes
- **Third-party module `mod-autobalance`** (azerothcore/mod-autobalance), pinned in `fork.lock` like mod-playerbots. It's fetched, linked into the core's `modules/`, built, and included in releases with its licence notice. `mod-solocraft` was evaluated and rejected; the reasons are in design.md §1.
- **ADR-0014 "Third-party gameplay modules: mod-autobalance"**, covering the choice, the pin, the licence, and the upstream search.
- **Config:** a Moonglade AutoBalance section (`autobalance` in `harness/hslib/config.py`) whose values are set from Zach's answers and the tuning runs. A `scaling-off` profile is added for counterexamples. The worldserver gets `AccountInstancesPerHour` raised.
- **mod-moonglade `MoongladeDungeons`:** when you enter a 5-player dungeon whose saved copy is fully cleared and has no real player inside, your (and your companions') save for it is cleared, so you get a fresh copy. This uses the core's own unbind API, so no core patch is needed. Config key `Moonglade.Dungeons.FreshAfterClear`, default 1.
- **Harness:** `harness/hslib/dungeon.py`, the dungeon driver that `m4-dungeon-companions` reuses. Every dungeon fact it uses (encounters, final boss, boss spawn, target level) comes from DBC or the world DB by rule. New `scale.*` and `lock.*` checks.

## Listed dungeons (the M4 set; the checks of all three changes use these)
All values come from the client's `LFGDungeons.dbc` and `DungeonEncounter.dbc` (client-data v20.0) and AzerothCore's `instance_encounters` (base SQL at our core pin). How they were read is in design.md §6.

| Dungeon | Map | LFG id | Levels (min–max, target) | Final boss (instance_encounters.lastEncounterDungeon) | Why it's on the list |
|---|---|---|---|---|---|
| The Deadmines | 36 | 6 | 15–25, 19 | Edwin VanCleef (entry 639) | Classic low-level dungeon, the first one most players see. mod-playerbots has **no** classic dungeon tactics, so this tests the generic tank, healer and DPS behaviour. Vanilla mode's pace matters most here. |
| Razorfen Downs | 129 | 20 | 32–42, 36 | Amnennar the Coldbringer (7358) | Classic mid-level dungeon that both factions can reach (the Barrens border), with four encounters and a gong event (AzerothCore `instance_razorfen_downs.cpp`, `GO_GONG`). It tests an event-gated encounter and a caster final boss with no dungeon tactics. Whether an event-summoned boss counts toward "finished" follows the rule in design.md §6. |
| Hellfire Ramparts | 543 | 136 | 57–67, 63 | Vazruden the Herald (17537) | The first Outland dungeon. mod-playerbots has a dedicated tactics module for it (`tbc-ramp`). |
| Utgarde Keep | 574 | 202 (heroic: 242) | 68–80, 72 | Ingvar the Plunderer (23954) | The first Northrend dungeon, with dedicated tactics (`wotlk-uk`). Its heroic version is the heroic-lockout test. |

The set covers four level bands and both dungeons with and without mod-playerbots tactics. It leaves out dungeons whose last boss needs a long escort or a group event (for example Wailing Caverns' Naralex escort). Raids are out of M4.

## Open questions for Zach (plain gameplay terms; the default ships if you don't answer)
1. **How hard should a dungeon be when you go in alone?** *Default:* about as hard as the dungeon is for a normal group at its suggested level. You'll need to play carefully and maybe drink a potion on bosses, but you won't need to be over-levelled. Alternatives: easier ("story": you can mostly fight through), or harder.
2. **Do companions make the dungeon harder, like real players would?** *Default:* yes. Each companion counts as one more person, so you plus four companions meet the normal, unchanged dungeon. The alternative is to count companions as "half a person", so a full party has it a bit easier than normal.
3. **If you go back to a low-level dungeon, should it level up to match you?** *Default:* no. Dungeons keep their own levels, so going back to the Deadmines at level 60 is easy, as in the original game. (The module can raise or lower monster levels to match yours. You'd turn that on if you wanted every dungeon to stay a challenge.)
4. **When the monsters are made weaker for a smaller party, should they give less XP and gold?** *Default:* yes. XP and gold drop in proportion, so a solo run isn't a faster way to level than questing (this matters for vanilla pace). The alternative is full XP and gold.
5. **Heroic dungeons:** *default:* no once-a-day lock; they reset the same way normal dungeons do. The alternative is to keep the daily lock for heroics only.

**Zach's answers (2026-10-10):**
- Q1 asked to confirm the meaning. The default stands: solo monsters are weakened so a fight feels like a normal group's fight at the suggested level. They are not left at full group strength.
- Q2: "We shouldn't make it even more difficult than vanilla". A party never meets anything harder than the unchanged dungeon (CurveCeiling 1.0, `scale.party_stock`), and smaller parties only ever get weaker monsters. Companions count as one person each (default).
- Q3: no level scaling (confirmed).
- Q4: less XP and gold when weakened (yes).
- Q5: no heroic daily lock (yes).
- Follow-up research (not in M4): with vanilla mode on, dungeon creatures keep their WotLK numbers (mod-era-profile covers outdoor 1–60 only). Comparing them with VMaNGOS dungeon values is a separate era-profile item.

## Sources
- mod-autobalance at `1d707cb61a0962ab01e4123681e2d3b66cc558a2` (2026-10-07) and mod-solocraft at `59588838988d1b74111d26670d99d0e649014b93` (2026-09-21). They were read from source tarballs; links and findings are in design.md §1.
- Upstream issue search: design.md §1.4.
- Dungeon facts: design.md §6 (DBC/SQL rules). No game data values are written by hand: this change adds no creature, item or quest values. The tuning knobs (inflection point, floor, ceiling) are module settings measured by runs, not game data, and each one records its reason.

## Risks
- **Headless driving (prototype 1.1).** No check so far has run a party inside an instance. It's unknown whether the probe can enter a dungeon by GM teleport, whether playerbots' "selfbot" mode moves and fights a headless character, and whether AutoBalance counts the bots as expected. If the selfbot route fails, the fallback is a passive probe master directing companions with `attack` orders (design.md §5).
- **Module build at our pins.** AutoBalance master needs AzerothCore APIs from 2025 (`SetStatFlatModifier`, `OnCreatureSelectLevel`), and our core pin has both (checked in source). It still has to build together with our three core patches, including the pause patch 0003.
- **Upstream bugs.** There are open AutoBalance crash and scaling issues (design.md §1.4). None of them hit the listed dungeons as far as the issue texts say, but #222 (a crash on respawning a transport creature) is in a code path every creature passes through.
- **Licence.** AutoBalance has no LICENSE file. Its main source file says GPL-2.0-or-later, and the other headers point to AzerothCore's licence. The release has to carry that notice (ADR-0014). This is compatible with our GPL-2.0-or-later.

## Spec-change log
- (none yet)
- 2026-10-10 (tightening, from prototype 1.1): new scenario "Dismissing companions keeps an unfinished dungeon" (`lock.partial_kept_dismissed`). Stock WotLK clears a player's non-permanent saves when the group breaks up (`Group.cpp:736` → `PlayerMisc.cpp:295`), so "unfinished keeps its progress" didn't hold once companions were dismissed.
- 2026-10-10 (new requirement, from prototype 1.1): "Companions can follow you into a fresh dungeon" (`lock.companion_unstuck`). A pool companion that logged out inside an old copy came back into that copy and couldn't be summoned out.
- 2026-10-10 (test method, from prototype 1.1): lockout checks give boss credit with `.damage <current health>`, not `.die` (no credit under `Die.Command.Mode=1`, `Unit.cpp:13554`). Dungeon checks that need level-3 GM commands use a separate level-3 harness account, never the companion account or PLAYER.
