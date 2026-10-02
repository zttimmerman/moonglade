# Tasks

## 1. Tiers (builder)
- [x] 1.1 `tier` on @check (default quick), runner `--tier`, results.json tier, full_run rule; tag existing checks — verify: `report.tiers`, `report.tiers_declared`, `report.full_run`
## 2. Smart clean build (builder)
- [x] 2.1 Input hash + stamp, from-empty vs verified-current, `--force-clean` on `hs run`/`hs build` — verify: `build.clean`, `build.clean_smart`
## 3. Instant logout (builder)
- [x] 3.1 Grant only the instant-logout permission to the test account; probe/fixture logouts get faster — verify: `fixture.instant_logout`
## 4. Verify
- [x] 4.1 Verifier (per the 2-full-runs budget): 1 full run with `--force-clean` (42/42), 2 quick runs (exit 0, ~36 s), ≥5 repeats of changed checks, counterexamples. PASS 2026-10-02
