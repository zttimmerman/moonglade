# Design: M3 Vanilla mode

## Context
Gate 1 decisions and data from `docs/gate1-era-profile-memo.md`; tool `tools/era-diff`.

## Decisions
- **Data flow:** `tools/era-diff` gets `--emit-sql`, which writes `modules/mod-era-profile/data/sql/db-world/era_*.sql`: side tables with vanilla values plus source citation columns (snapshot, table, entry). It's regenerated, never hand-edited. `era.scope_guard` cross-checks the counts against the report.
- **Apply mechanism (in order of preference):**
  1. Runtime hooks: `OnPlayerQuestComputeXP` for quest XP (confirmed in the Gate 1 work). Other hooks get researched in AC's ScriptMgr (e.g. XP-for-level, creature template/HP selection, item/trainer requirements).
  2. Startup apply/revert. Where no hook exists, the module captures a baseline table once, then at server start writes either the vanilla or the baseline values for exactly the in-scope rows, before the world data loads. It's idempotent, so a crash mid-switch heals on the next start.
  3. A core patch only with an ADR.
- **Config:** `EraProfile.Enable` lives in the module's `.conf.dist` (default 1). `harness/hslib/config.py` renders it, and harness profiles can override it for the toggle checks.
- **Future:** keep the apply logic per entity and parameterised by mode, so a per-character runtime mode (the backlog menu) can reuse it.

## Risks
- What VMaNGOS `RewXP` means is unverified → must be confirmed before quest XP ships (proposal: Data provenance).
- Startup-apply races with world data load. → The module must run its apply in a hook that fires before the relevant ObjectMgr loads; verified by `era.default_on` and the spot checks.
- Death Knights level 55–59 slower (known, accepted at Gate 1).
