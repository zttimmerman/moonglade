# Tasks

## 1. Patch (builder)
- [x] 1.1 Write the patch against core_sha (minimal); confirm it applies via `hs build`; ADR-0006 → accepted — verify: `core.patch_applied`
## 2. Evidence (builder)
- [x] 2.1 Baseline repro on an unpatched scratch build (separate build dir) — verify: `core.login_logout_race_baseline`
- [x] 2.2 Patched server survives the race — verify: `core.login_logout_race`
- [x] 2.3 Decide with evidence whether `settle_login` stays (defense in depth) — record in design.md
## 3. Verify
- [x] 3.1 Verifier: 1 full run (--force-clean) 45/45 + 5x core.*/pin_enforced. PASS 2026-10-02 (spec narrowed per its concern 1; F11 follow-up)
