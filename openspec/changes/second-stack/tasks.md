## 1. Lane layer
- [x] 1.1 paths.py lane layer, refusal outside harness runs, lane test switches + refusal
- [x] 1.2 config/stack: lane confs, WorldServerPort/RealmServerPort rendered, AC_* env, atomic render, -c process matching, realmlist port from P
- [x] 1.3 db/launch/procs/runreg/nightly/play GIVENs lane-aware; Wow rule prefix match
- [x] 1.4 `hs lane init|status|down|reset`; template datadir + per-run APFS clone; Wine prefix clone
## 2. Scheduler
- [x] 2.1 lanes.py table + split + memory preflight; lane worker; runner barriers, merge, meta.lanes; `--lanes`
- [x] 2.2 mid-run pressure fallback
## 3. Checks and timing
- [x] 3.1 checks lane.paths, lane.isolation, lane.refusal, lane.independent_down, lane.orphans, lane.table, lane.merge, lane.preflight, report.lanes (each with a counterexample)
- [x] 3.2 timing guard lane-mode label; hard-coded port asserts read P
- [x] 3.3 docs: harness skill + design.md as built
