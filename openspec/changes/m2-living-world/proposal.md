# Proposal: M2 Living world: population, companions, performance budget

## Why
KICKOFF §8 M2: world population bots are active in starting zones and cities, party companion bots are usable for questing, and a performance budget is defined and met on the target Mac. M0 runs only 10 random bots, with no companions and no budget.

## What Changes
- A **population profile** for mod-playerbots (rendered by `harness/hslib/config.py`): bot count, level distribution and activity settings that put visibly active bots in every starting zone and capital, sized to the budget.
- **Companions:** the player adds class-chosen bots to their party, and they follow, fight alongside, and share quests. For headless testing, the probe gains chat send/receive (CMSG_MESSAGECHAT / SMSG_MESSAGECHAT), so it can act as a player who issues `.playerbots bot …` commands and whispers bots.
- A **performance budget** with numbers, measured by the harness on this Mac (M2 Pro, 16 GB): server memory, world update time, host free memory. Plus a human check of client smoothness.
- Harness: per-profile module config overrides, a `Playerbots.log` grep helper, and a shared population warm-up so the population checks measure the same session.

## Capabilities
### New Capabilities
- `world-population`: ambient bot population in starting zones and capitals.
- `companion-bots`: player-controlled party bots for questing.
- `performance-budget`: resource limits for the whole stack on the target Mac.

### Modified Capabilities
- `harness-reporting`: slow-tier scenario checks (skipped in full runs, required in slow runs and at merge for changes that reference them). The M0 bot cap of 10 becomes the `m0` profile; the population profile is new config. `build.playerbots_active` is unaffected.

## Impact
`harness/hslib/config.py` (population profile), probe chat, new checks. The population and budget checks are tagged `slow` (≈20 min warm-up + sampling), so full runs stay fast; slow runs and this change's merge gate run them. No core patches expected; any that turn out to be needed get an ADR.

## Data provenance
Zone, area and map IDs used by checks come from the client's `AreaTable.dbc`/`Map.dbc` (`data/client-data/dbc`) or `game_tele`, read at runtime and cited in check evidence, never from memory (CLAUDE.md §7).

## Spec-change log
- 2026-10-02 · (pre-build) Population/perf checks are tagged `slow` instead of growing every full run by ~20 min. Adds the slow-tier scenario rule (harness-reporting delta). The guarantee is unchanged at merge: this change must pass an unfiltered `--tier slow` run.
- 2026-10-03 · **Performance budget revised after measurement (Zach chose it).** The pre-measurement guess of worldserver ≤ 4.0 GB is unreachable at any useful size: playerbots has a ~3 GB fixed cost (worldserver footprint 1.5 GB with bots disabled; 4.21 GB at N=50; 4.64 GB at N=300; ~2–3 MB per extra bot). The "host free+inactive ≥ 3 GB" limit measured the user's other apps (Chrome/Blender/Plex; 7–8 GB swap), not the stack. New limits: worldserver ≤ 6.0 GB, stack total ≤ 7.0 GB, mysqld and update time unchanged, with host memory recorded only. Memory is measured as max(RSS, footprint), since RSS undercounts under swap. Client smoothness stays a human check.
- 2026-10-03 · **Default population about 500 (Zach),** to make capitals reliably populated (at N=300 a capital swung 0–4). Added requirements: world size is one easy setting with presets, `./play` defaults to the living world, scale-down is trivial and is planned as a front-menu option; and a fresh bot world each session (Zach chose this over a persistent world for now), scoped to bot accounts only.

