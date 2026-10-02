# Tasks

## 1. Tiers (builder)
- [x] 1.1 `tier` on @check (default quick), runner `--tier`, results.json tier, full_run rule; tag existing checks — verify: `report.tiers`, `report.tiers_declared`, `report.full_run`
## 2. Smart clean build (builder)
- [x] 2.1 Input hash + stamp, from-empty vs verified-current, `--force-clean` on `hs run`/`hs build` — verify: `build.clean`, `build.clean_smart`
## 3. Instant logout (builder)
- [x] 3.1 Grant only the instant-logout permission to the test account; probe/fixture logouts get faster — verify: `fixture.instant_logout`
## 4. Verify
- [ ] 4.1 Verifier: ≥2 full runs, one with `--force-clean`; quick-tier timing; counterexamples
