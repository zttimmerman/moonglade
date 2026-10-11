# ADR-0014: Third-party gameplay module: mod-autobalance

- Status: accepted (2026-10-11, `m4-dungeon-scaling` verifier cycle 2 PASS; cycle 1 full run 216/2/35 at d123522, both failures fixed and re-verified). Checks: `scale.module_loaded`, `scale.release_notice`, `build.pin_enforced`, `build.clean_smart`.
- Date: 2026-10-10

## Context
- M4 ("Solo tuning", KICKOFF §8) needs dungeons that scale to one player plus N companions. CLAUDE.md §2 names "a SoloCraft-style module (evaluate the existing one before writing new)".
- The evaluation is `openspec/changes/m4-dungeon-scaling/design.md` §1. Two existing modules were read from source:
  - **mod-autobalance** (`azerothcore/mod-autobalance`, master `1d707cb61a0962ab01e4123681e2d3b66cc558a2`, 2026-10-07). It scales the **creatures** of an instance by the number of non-GM players in the map: health, mana, armour, damage, CC duration, and optionally level, XP and money. It re-evaluates as players join or leave, outside combat.
  - **mod-solocraft** (`azerothcore/mod-solocraft`, master `59588838`, 2026-09-21). It buffs the **players** on entry, divided by the group size at that moment, and stores per-character rows in a custom characters-DB table.
- Prototype task 1.1 (branch `spike/m4-dungeon`, design.md "Prototype results") built AutoBalance at that commit with our core pin `7f12e89e`, core patches 0001–0003, playerbots `7bae1b5c` + 0001, mod-era-profile and mod-moonglade. It compiled without source changes, ran for about 2 h with no crash and an empty `Errors.log`, and counted playerbots companions as players (1 / 3 / 5 players gave multipliers 0.123 / 0.684 / 1.000 at the module defaults).

## Decision
1. **Use mod-autobalance, unmodified, pinned in `fork.lock`** (`autobalance_repo`, `autobalance_branch=master`, `autobalance_sha=1d707cb6…`), the same way as mod-playerbots:
   - `hs build` clones it to `vendor/mod-autobalance`, checks out the pin, cleans the tree and links it into the core's `modules/`. `build.pin_enforced` rejects a moved or dirty checkout.
   - `autobalance_repo/branch/sha` are build pins, so a bump forces a from-empty build (`build.clean_smart`).
   - No patch queue is created. If a patch is ever needed, it gets a `patches/autobalance/` queue like ADR-0008 and its own ADR.
   - Moonglade's settings live in the `autobalance` section of `harness/hslib/config.py` and are rendered into `server/etc/modules/AutoBalance.conf` (design.md §2).
2. **Why not SoloCraft:**
   - Companions summoned after entry get its "overload" debuff, and our normal flow is to enter, then summon.
   - It buffs the bots too.
   - Its custom characters-DB table would join the character save set, which AutoBalance avoids (it stores no character data).
3. **Why not our own module:** it would repeat AutoBalance's per-creature bookkeeping (summons, clones, vehicles, level scaling) for no gain.
4. **Why master, not the `stable` tag:** `stable` (`5d2778e3`, 2024-09-10) predates the core API renames our pin has (`OnCreatureSelectLevel`, AutoBalance #213; AzerothCore PR 23346, AutoBalance #217), so it would not compile against our core.

## Licence
- There is no LICENSE file in the module. `src/AutoBalance.cpp` carries a GPL v2 "or (at your option) any later version" header (copyright AzerothCore 2018, CVMagic 2012, TrinityCore 2008-2010, ScriptDev2 2006-2009, KalCorp 1985-2010). The other source files carry the AzerothCore one-line header pointing to AzerothCore's LICENSE (GPL-2.0).
- Both are compatible with Moonglade's GPL-2.0-or-later (ADR-0012).
- A release's `THIRD_PARTY/mod-autobalance/` holds the quoted `src/AutoBalance.cpp` header and a note that the other files are under AzerothCore's licence (whose text ships in `THIRD_PARTY/azerothcore/`). The release source manifest carries the module's source tree at the pin, and the payload carries the rendered module config. `scale.release_notice` checks all of this.

## Upstream status (search 2026-10-10)
`gh search issues|prs` on `azerothcore/mod-autobalance`, `mod-playerbots/mod-playerbots` and `mod-playerbots/azerothcore-wotlk` (design.md §1.4):
- Build issues #220 and #214 affect older cores; ours has the needed APIs.
- Crash #222 (open) is a transport creature's respawn on map 610. None of the M4 dungeons have transports. Watched in the full runs.
- Scaling oddities #198 (under-levelled over-downscaling, needs level scaling, which we leave off), #201 (XP when scaled up, which we don't do), #215 (`Enable.Global` off), #224 (Dire Maul): none on our settings or dungeon list.
- No AutoBalance issues in the playerbots repos.

## Fallback
- If the pin breaks with a future core bump: hold the core pin, or bump AutoBalance to a commit that follows the core, recorded here.
- If an upstream bug hits a listed dungeon: per-dungeon disable through AutoBalance's own config (`AutoBalance.Disable.PerInstance`) first; then cherry-pick an upstream fix into a `patches/autobalance/` queue; then our own patch, each with an ADR.
- Turning scaling off entirely is one setting (`AutoBalance.Enable.Global = 0`, the `scaling-off` profile), with no data to undo.

## Consequences
- A third gameplay module (after mod-playerbots and our own) is built into every server and release.
- Dungeon creatures are weaker for parties under five; a full party of five meets the unchanged dungeon (`CurveCeiling 1.0`), so nothing is ever harder than stock.
- AutoBalance's chat notices are off (`PlayerChangeNotify = 0`, the seamless-experience rule).
- A future mod-autobalance bump is a reviewed fork.lock change with a from-empty build and the `scale.*` checks.
