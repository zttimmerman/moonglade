# Tasks

## 1. Patch (builder)
- [ ] 1.1 Write the patch against core_sha (minimal); confirm it applies via `hs build`; ADR-0006 → accepted — verify: `core.patch_applied`
## 2. Evidence (builder)
- [ ] 2.1 Baseline repro on an unpatched scratch build (separate build dir) — verify: `core.login_logout_race_baseline`
- [ ] 2.2 Patched server survives the race — verify: `core.login_logout_race`
- [ ] 2.3 Decide with evidence whether `settle_login` stays (defense in depth) — record in design.md
## 3. Verify
- [ ] 3.1 Verifier: 1 full run (--force-clean, since a patch changes build inputs) + ≥5 repeats of core.*
