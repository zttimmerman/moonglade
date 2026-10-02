# Tasks

## 1. Data (builder)
- [ ] 1.1 Confirm VMaNGOS `RewXP` semantics from VMaNGOS source (cite file/line); update memo; if unconfirmable, flag and leave quest XP pending
- [ ] 1.2 `era_diff.py --emit-sql` → side tables with citations; deterministic output — verify: re-run produces identical SQL
## 2. Module (builder)
- [ ] 2.1 `modules/mod-era-profile` skeleton, config, startup log line, hook research notes in design.md — verify: `era.default_on`
- [ ] 2.2 XP curve + quest XP — verify: `era.xp_curve`, `era.quest_xp`
- [ ] 2.3 Elites + HP — verify: `era.elites`
- [ ] 2.4 Mounts + riding — verify: `era.mounts`
- [ ] 2.5 Toggle round-trip + scope guard — verify: `era.toggle_roundtrip`, `era.scope_guard`
## 3. Verify + human
- [ ] 3.1 Verifier pass + full harness
- [ ] 3.2 Human: Zach plays vanilla mode
