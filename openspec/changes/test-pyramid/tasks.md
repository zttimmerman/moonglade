Estimates are agent-hours of builder work, without padding (recent changes ran 2–3× faster than their estimates). Runtime work happens in the main checkout, which needs the stack, and one agent owns the stack at a time. Schedule: after m4-dungeon-scaling merges and before m4-dungeon-companions.

## 1. Measure and prototype (go/no-go before any migration)
- [x] 1.1 Unit-runner prototype (≈ 3 h).
  - `hs test` skeleton: `unittest` discovery, per-test timing, results record, the 2 s cap.
  - 20 real Python unit tests: backups rotation, `lanes.preflight_decision`, the runner's selection and budget rules.
  - The Lua 5.1.5 pin (compute the sha256 and cross-check it against lua.org's published checksum; `fork.lock` + `downloads.PINS`), built with the system `cc` into `env/tools/`.
  - The `wowstub.lua` core, the XML prelude, and 5 front-menu tests, including the 60 s simulated no-login.
  - `src/logic/SavesLogic` with `CleanLabel` and the retention choice, called from MoongladeSaves.cpp, plus 10 C++ tests via `mgtest.h` and clang++.
  - Confirm with an incremental server build that the module picks up `src/logic/`.
  - **Measure and record in design.md §12:** per-test times per suite, the cold and cached C++ build, the Lua build, and the unit tier's wall time with all 53 static checks (serial and 4-process pool).
  - **Pass criteria:** each test ≤ 2 s; projected unit layer ≤ 60 s warm.
- [x] 1.2 Shared-server integration slice (≈ 3 h).
  - The fixture: one `hs up`, per-test characters, reset with leak detection, undo registration, timed resets.
  - `pause.freeze_matrix` (5 pause checks → 1) and `pause.disconnect_unpauses` with a per-test character.
  - Respawn on a scratch spawn: first find the pinned core's GM command for a spawn's respawn time, with the source file cited.
  - 4 saves tests as `scope="character"`.
  - Check whether `.reload config` applies `Moonglade.Pause.Enable`.
  - **Run the slice 3× serially, 3× with the 2-worker pool, and once in reverse order.**
  - **Pass criteria:** identical statuses; reset median ≤ 1 s, max ≤ 3 s; each ported test ≤ its projection in design.md §9.3 + 20 %; the pool's saving is recorded.
  - Also measure the fallback levers (passive observer, shared scratch play client, `lifecycle.persist` in the era restart).
- [x] 1.3 Selector prototype and replay (≈ 1.5 h).
  - Covers + closure selection, FULL triggers, the no-impact list, the `config.py`/`paths.py` constants rule.
  - `hs select --replay 15` over #19–#33, priced with the last full run.
  - Record the selection time (≤ 10 s), each PR's selection and reason, and the mean. Compare with design.md §8's rough table.
- [x] 1.4 Go/no-go note (≈ 0.5 h).
- [x] 1.5 Redesign re-measure (design §13): ownership selector + narrower FULL rule, replay #19–#33 + #1–#3 of the new repo; pass = replay mean ≤15 min, selection ≤10 s (≈ 1.5 h).
- [x] 1.6 Clustered integration slice: saves cluster (manual, quick, undo, inworld load in one session) and dungeon cluster (`dng.session`) with bulk fixtures; pass = each session ≤60 s, statuses identical 3× serial / 3× pool / 1× reversed, bulk fixtures ≤10 s (≈ 2.5 h).
- [x] 1.7 Go/no-go 2: projected complete suite ≤30 min with the measured numbers; stop and report on a miss (≈ 0.5 h).
  - Write the measured numbers into design.md §12 and the proposal's log.
  - If a pass criterion fails (unit per-test time, reset cost, pool flakes, or a selection over 10 s), stop and report to the orchestrator with the numbers before task 2. Don't migrate on a failed prototype.

## 2. Harness engine
- [ ] 2.1 Layers and run tiers (≈ 2.5 h).
  - `layer`, `covers`, `scope`, `budget_s` and `parallel_safe` in the registry.
  - `--tier unit|pr|full`, plus `hs test`.
  - Remove the `quick|slow|gui|backups` run tiers and their skip rules.
  - Order the segments: cold → `hs up` → shared → profile groups → warm journeys → `hs down` → `first_play` → gate → reports.
  - Results schema: `meta.layers`, `meta.selection`, `meta.unit`, per-record `layer`.
  - Every existing check gets an interim layer: `stack="down"` checks → cold integration, `stack="up"` → shared integration, slow → gate, `quick` and no-stack → unit.
  - Checks: `report.layers_declared`, `report.unit_layer`, `report.gate_rule`.
- [ ] 2.2 Impact selection (≈ 2 h).
  - The production selector from 1.5 (declared ownership, design §13), `report.ownership`, fail-safe cases, selection timing, `meta.selection`.
  - Checks: `report.selection`, and `hs select --replay`.
- [ ] 2.3 Budgets and reports (≈ 2 h).
  - Per-layer timing.
  - `report.layer_budgets`, `report.pr_average` (`pr-runs.jsonl`, the replay fallback, the merge-with-remedy note), `report.journeys`, `report.no_fixed_sleeps` (lint plus allowlist).
  - `report.coverage` accepts `unit:` ids.
  - `report.timing` per layer.
  - Remove `report.tiers`' nested run and `hs nightly` (code, CLI, spec checks).
- [ ] 2.4 Shared-server fixture, production version (≈ 2 h).
  - Profile groups, the 2-worker pool (only if 1.2 passed), end-of-run character cleanup, `HS_PLAY_KEEP_STACK` with its refusals.
  - Checks: `fixture.shared_server`, `fixture.reset_leak`, `fixture.keep_stack_refused`.
  - Each new check gets a counterexample run.

## 3. Migration slices
Each slice is done the same way:
1. Measure the area today.
2. Port its tests to the layers in appendix A.
3. Prove each new test fails on a counterexample.
4. Re-point the capability spec's `Check:` lines (MODIFIED delta + a spec-change log entry).
5. Delete the old checks.
6. Run the area 3×.
7. Record before/after.

The builder runs `hs test` plus the slice's own tests; no acceptance tier per task.

- [ ] 3.1 backups (≈ 2 h).
  - Unit: rotation, manifest, refusal reasons, notice text.
  - Cold: on-quit + one injected failure.
  - Shared: kill-safe and restore driven directly.
  - `app.backups_window` waits for 3.5.
- [ ] 3.2 saves + savesctl (≈ 3.5 h).
  - Finish `SavesLogic`: JSON, labels, retention, list format, refusal reasons, combat/dead predicate.
  - Lua tests for `MoongladeSaves.lua` and `HSCharSaves.lua`.
  - Saves integration with the pool.
  - The `savesctl` protocol as unit tests.
  - `journey.saves`.
- [ ] 3.3 client (≈ 1.5 h).
  - Resolution decision and config gate as unit tests.
  - Warm config-gate run.
  - `journey.stock_client`.
  - Drop `client.launch_smoke`.
- [ ] 3.4 pause (≈ 2.5 h).
  - `PauseLogic` (state machine, cooldown arithmetic) and its C++ tests.
  - Production freeze matrix and respawn.
  - The disabled profile group.
  - Lua tests for the add-on's menu hooks and banner.
  - `journey.pause`.
- [ ] 3.5 app (≈ 2 h).
  - `journey.app_desktop` (GUI, stand-in, relocated copy, Backups window restore).
  - Bundle and icon checks to the unit layer.
- [ ] 3.6 login (≈ 3 h).
  - Lua suites for `HSFrontMenu` and `HSSeamless` (Continue, overlay bar, backdrop choice, no-login clock).
  - `journey.first_play` and `journey.login_recovery`.
  - The spec-change log entry for the 60 s → simulated + 10 s live move.
- [ ] 3.7 play (≈ 1.5 h).
  - Sessions S1–S3, the first-run account.
  - Signal and lock logic as unit tests.
- [ ] 3.8 The rest (≈ 2 h).
  - Lifecycle, control, fixtures, era (merged restarts), comp, core, lane (rules as unit tests, real stacks as gate), report, build, firstlaunch, release, signing, power, re, public, repo, setup.
  - Layer tags and `covers`; `lifecycle.isolated` narrowed to our pids.
- [ ] 3.9 Dungeons, after m4-dungeon-scaling merges (≈ 1 h).
  - `DungeonsLogic` with its C++ tests.
  - Its checks placed in layers with `covers`.

## 4. Rules and docs
- [ ] 4.1 CLAUDE.md §4, §6, §9 and §10 with the proposal's exact text (orchestrator; Zach approved the change).
  - `hs-change-loop` run budget: builder = unit + own test; verifier = one PR-tier run + 3× new/changed + 2–3 spot checks; full ×3 only at milestone gates.
  - `hs-harness-checks`: layers, `covers`, `scope`, no fixed sleeps, unit-test ids.
  - (≈ 1 h)
- [ ] 4.2 design.md §12 "As built", with measured per-layer times; the timing baseline rejoined from the verifier's full run (≈ 0.5 h).
- [ ] 4.3 Ask Zach to run `harness/hs nightly --uninstall` if he installed the LaunchAgent (agents can't); record the answer in STATUS.md (≈ 0 h; human step).

## 5. Verification
- [ ] 5.1 Verifier (fresh context; ≈ 2.5 h, mostly run time).
  - `hs test` ×3 (each ≤ 120 s).
  - One `--tier pr` run on this change (FULL by rule, since `stack.py` and the probe users change).
  - `--tier full` ×3 as the flake sweep, because every test moved.
  - Every scenario PASS or FAIL with evidence.
  - Each budget compared with its target; a miss is a FAIL.
  - Spot-check 3 counterexamples (one per runner: Python, C++, Lua) and one leaked-state case.

**Total: ≈ 39.5 agent-hours.**

| Group | Hours |
|---|---:|
| Measure and prototype | 8 |
| Engine | 8.5 |
| Migration | 19 |
| Rules and docs | 1.5 |
| Verification | 2.5 |

At recent speed, that is likely about 15–20 agent-hours of builder time. The wall time is longer, because only one agent can own the stack.
