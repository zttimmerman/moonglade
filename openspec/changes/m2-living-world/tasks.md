# Tasks

## 1. Harness support (builder)
- [ ] 1.0 Slow-tier scenario rule in runner/reporting — verify: `report.slow_tier_rule`
- [ ] 1.1 Config profiles (m0 / population) with module overrides; `Playerbots.log` grep helper; DBC reader moved into `hslib/dbc.py` — verify: unit use by pop checks; `hs list` shows new checks
## 2. Population (builder)
- [ ] 2.1 Population profile + checks `pop.zones_covered`, `pop.size_stable`, `pop.bots_active`
## 2b. Settings + retune (builder)
- [ ] 2.2 World-size setting (presets, gitignored settings file, `./play` default population), retune to ~500, `pop.size_setting`, `pop.fresh_start_scope`; re-measure the budget at the new limits

## 3. Companions (builder)
- [ ] 3.1 Probe chat send/receive (AC-source cited)
- [ ] 3.2 Checks `comp.add_remove`, `comp.follows`, `comp.assists`, `comp.quest_shared`
## 4. Budget (builder)
- [ ] 4.1 `perf.server_budget` sampler + `perf.population_tuned`; tuning record `docs/research/m2-population-tuning.md`
## 5. Verify + human
- [ ] 5.1 Verifier pass + full harness
- [ ] 5.2 Human: living world, companions, playability (3 `[human-verify]` scenarios)
