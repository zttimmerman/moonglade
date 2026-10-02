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
(none; the M0 bot cap of 10 is replaced by the population profile, which is a config change. `build.playerbots_active` is unaffected.)

## Impact
`harness/hslib/config.py` (population profile), probe chat, new checks. Every full run grows by a population warm-up (≈10 min). No core patches expected; any that turn out to be needed get an ADR.

## Data provenance
Zone, area and map IDs used by checks come from the client's `AreaTable.dbc`/`Map.dbc` (`data/client-data/dbc`) or `game_tele`, read at runtime and cited in check evidence, never from memory (CLAUDE.md §7).

## Spec-change log
(none yet)
