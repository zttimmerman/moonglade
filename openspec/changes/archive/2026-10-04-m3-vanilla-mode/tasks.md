# Tasks

## 1. Data (builder)
- [x] 1.1 Confirm VMaNGOS `RewXP` semantics from VMaNGOS source (cite file/line); update memo; if unconfirmable, flag and leave quest XP pending
- [x] 1.2 `era_diff.py --emit-sql` → side tables with citations; deterministic output — verify: re-run produces identical SQL
## 2. Module (builder)
- [x] 2.1 `modules/mod-era-profile` skeleton, config, startup log line, hook research notes in design.md — verify: `era.default_on`
- [x] 2.2 XP curve + quest XP — verify: `era.xp_curve`, `era.quest_xp`
- [x] 2.3 Elites + HP — verify: `era.elites`
- [x] 2.4 Mounts + riding — verify: `era.mounts`
- [x] 2.5 Toggle round-trip + scope guard — verify: `era.toggle_roundtrip`, `era.scope_guard`
## 3. Verify + human
- [x] 3.1 Verifier pass + full harness (verifier PASS at 22ef40e: slow --force-clean 68/0, era.* 7/7; re-verify PASS at 028fecd after B17: full --force-clean 61/0, era.* 5/5, sabotage cases)
- [ ] 3.2 (deferred to Zach's next play session; tracked in STATUS.md) Human: Zach plays vanilla mode
