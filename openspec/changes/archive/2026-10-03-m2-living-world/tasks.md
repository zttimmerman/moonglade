# Tasks

## 1. Harness support (builder)
- [x] 1.0 Slow-tier scenario rule in runner/reporting — verify: `report.slow_tier_rule`
- [x] 1.1 Config profiles (m0 / population) with module overrides; `Playerbots.log` grep helper; DBC reader moved into `hslib/dbc.py` — verify: unit use by pop checks; `hs list` shows new checks
## 2. Population (builder)
- [x] 2.1 Population profile + checks `pop.zones_covered`, `pop.size_stable`, `pop.bots_active`
## 2b. Settings + retune (builder)
- [x] 2.2 World-size setting (presets, gitignored settings file, `./play` default population), retune to ~500, `pop.size_setting`, `pop.fresh_start_scope`; re-measure the budget at the new limits

## 3. Companions (builder)
- [x] 3.1 Probe chat send/receive (AC-source cited) — verified through comp.follows / comp.assists (chat commands, system-message parsing, party state)
- [x] 3.2 Checks `comp.add_remove`, `comp.follows`, `comp.assists`, `comp.quest_shared` — all pass in the unfiltered slow run at 2026-10-03T13:58Z (60/60), with core patch 0002 + playerbots patch 0001
## 3b. Gap fixes (builder B13)
- [x] 3.3 Core patch 0002: playerbots OnPacketReceived hook gets an intact packet for typed handlers (ADR-0007) + opcode audit. Verify: `comp.quest_shared`, patch-applied coverage
- [x] 3.4 Playerbots patch queue (`patches/playerbots/`) + patch 0001: `bot remove` removes the bot from the group (ADR-0008). Verify: `comp.add_remove`, pin/hash coverage

## 4. Budget (builder)
- [x] 4.1 `perf.server_budget` sampler + `perf.population_tuned`; tuning record `docs/research/m2-population-tuning.md`
## 5. Verify + human
- [x] 5.1 Verifier pass + full harness (verifier PASS at eca9cb7: slow run 60/0 + 5× comp/core; re-verify PASS at a01699a: B14 items, quick tier, client/play ×2)
- [ ] 5.2 (deferred to Zach's next play session; tracked in STATUS.md) Human: living world, companions, playability (3 `[human-verify]` scenarios)
