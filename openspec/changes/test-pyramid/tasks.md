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
- [x] 2.1 Layers and run tiers (≈ 2.5 h).
  - `layer`, `covers`, `scope`, `budget_s` and `parallel_safe` in the registry.
  - `--tier unit|pr|full`, plus `hs test`.
  - Remove the `quick|slow|gui|backups` run tiers and their skip rules.
  - Order the segments: cold → `hs up` → shared → profile groups → warm journeys → `hs down` → `first_play` → gate → reports.
  - Results schema: `meta.layers`, `meta.selection`, `meta.unit`, per-record `layer`.
  - Every existing check gets an interim layer: `stack="down"` checks → cold integration, `stack="up"` → shared integration, slow → gate, `quick` and no-stack → unit.
  - Checks: `report.layers_declared`, `report.unit_layer`, `report.gate_rule`.
- [x] 2.2 Impact selection (≈ 2 h).
  - The production selector from 1.5 (declared ownership, design §13), `report.ownership`, fail-safe cases, selection timing, `meta.selection`.
  - Checks: `report.selection`, and `hs select --replay`.
- [x] 2.3 Budgets and reports (≈ 2 h).
  - Per-layer timing.
  - `report.layer_budgets`, `report.pr_average` (`pr-runs.jsonl`, the replay fallback, the merge-with-remedy note), `report.journeys`, `report.no_fixed_sleeps` (lint plus allowlist).
  - `report.coverage` accepts `unit:` ids.
  - `report.timing` per layer.
  - Remove `report.tiers`' nested run and `hs nightly` (code, CLI, spec checks).
- [x] 2.4 Shared-server fixture, production version (≈ 2 h).
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

- [ ] 3.1 backups (≈ 2 h). _Part 1 (2026-10-11): unit done (rotation as a unit check with the dump faked in-process; manifest/verify, refusal reasons, notice text as Python unit tests). Left for part 2: kill-safe and restore driven directly on the shared server (still cold `./play` sessions), `app.backups_window` (3.5)._ _Part 2 (2026-10-11): not migrated further: kill-safe and restore still run their own cold `./play` sessions (517 s); `app.backups_window` is a child of `journey.app_desktop`._ _Part 3 (2026-10-11): kill-safe, failure and refusals are scenarios of `play.cold_sessions` (shared sessions per ending); restore_roundtrip without a client build; restores load the three DBs in parallel. `on_quit` still its own cold session (73.5 s)._
  - Unit: rotation, manifest, refusal reasons, notice text.
  - Cold: on-quit + one injected failure.
  - Shared: kill-safe and restore driven directly.
  - `app.backups_window` waits for 3.5.
- [ ] 3.2 saves + savesctl (≈ 3.5 h). _Part 1 (2026-10-11): integration clustered on fixture identities (`saves.session_a` and `session_b` pooled, `session_interval`, `scope` and `load_ordering` on their own identity); `savesctl.load_verb`/`build_refused` and `saves.addon_static` in the unit layer. Left for part 2: the rest of `SavesLogic`, the Lua add-on/glue tests, `journey.saves` (the live savesctl/charselect checks still run as legacy client checks, counted as journey time)._ _Part 2 (2026-10-11): `SavesLogic` gained JSON, SQL values, labels, the refusal predicate and the startup reason (6 C++ tests; `ListGen` and the save-validation half not extracted); `unit:lua:test_addon_saves.*` (12) for MoongladeSaves.lua (HSCharSaves.lua not yet); `journey.saves` (warm; 194.6 s against 150); `load_ordering` not pooled (its profile check is now a world restart)._ _Part 3 (2026-10-11): `savesctl.hygiene` warm (40 → 15 s); ListGen/validation and pooled load_ordering not done (timeboxed); journey.saves 190.8 s (miss)._
  - Finish `SavesLogic`: JSON, labels, retention, list format, refusal reasons, combat/dead predicate.
  - Lua tests for `MoongladeSaves.lua` and `HSCharSaves.lua`.
  - Saves integration with the pool.
  - The `savesctl` protocol as unit tests.
  - `journey.saves`.
- [ ] 3.3 client (≈ 1.5 h). _Part 2 (2026-10-11): `journey.stock_client` (warm, 32.1 s against 60); `client.resolution_default` is a child of `journey.first_play` (its unit half not split out); config gate not warm; `client.launch_smoke` kept: its client-connectivity scenario still names it and no delta removes it._ _Part 3 (2026-10-11): `client.config_gate` warm (70 → 18 s); `launch_smoke` kept (its scenario names it)._
  - Resolution decision and config gate as unit tests.
  - Warm config-gate run.
  - `journey.stock_client`.
  - Drop `client.launch_smoke`.
- [x] 3.4 pause (≈ 2.5 h). _Part 1 (2026-10-11): `PauseLogic` + C++ tests, `pause.freeze_matrix` (one 20 s pause), the scratch-spawn respawn, `disconnect_unpauses` on its own identity, the pause-off profile group, the gate soak, warm `journey.pause`. Left for part 2: the Lua tests for the add-on's menu hooks and banner._ _Part 2 (2026-10-11): the Lua tests for the menu hooks, banner and dim (`unit:lua:test_addon_pause.*`, 9, on the in-game stub `addonstub.lua`)._
  - `PauseLogic` (state machine, cooldown arithmetic) and its C++ tests.
  - Production freeze matrix and respawn.
  - The disabled profile group.
  - Lua tests for the add-on's menu hooks and banner.
  - `journey.pause`.
- [ ] 3.5 app (≈ 2 h). _Part 2 (2026-10-11): `journey.app_desktop` (cold; the app refuses the keep-stack switch) runs the former app sessions back to back: 453.5 s against 240; two GUI GIVENs failed in both runs because another app (Chrome, Claude) took activation._ _Part 3 (2026-10-11): names_running on the shared app session; single_instance (a) and the shutdown backup stage on the dist lifecycle session: 453.5 → 327.4 s (budget 240, miss; backups_window 147 s)._
  - `journey.app_desktop` (GUI, stand-in, relocated copy, Backups window restore).
  - Bundle and icon checks to the unit layer.
- [ ] 3.6 login (≈ 3 h). _Part 2 (2026-10-11): `journey.first_play` (cold session, then warm: 229.1 s against 150) and `journey.login_recovery` (warm: 151.2 s against 120); the 60 s no-login watch is 10 s live plus the simulated 60 s Lua test run inside the scenario._ _Part 3 (2026-10-11): first_play's Continue scenarios and first_start_settings' real run share the cold session: 229.1 → 127.0 s (pass); login_recovery 147.8 s (miss)._
  - Lua suites for `HSFrontMenu` and `HSSeamless` (Continue, overlay bar, backdrop choice, no-login clock).
  - `journey.first_play` and `journey.login_recovery`.
  - The spec-change log entry for the 60 s → simulated + 10 s live move.
- [ ] 3.7 play (≈ 1.5 h). _Part 2 (2026-10-11): not done (S1–S3 not merged: 379 s); with lane B refused its play.* checks now run after `hs down`._ _Part 3 (2026-10-11): `play.cold_sessions` (one session per ending; SIGTERM as a unit test): play + backups 896 → 504 s._
  - Sessions S1–S3, the first-run account.
  - Signal and lock logic as unit tests.
- [ ] 3.8 The rest (≈ 2 h). _Part 2 (2026-10-11): era off/on restarts shared (`era.session_toggle`, 114.6 → 57.4 s); lane.merge, lane.preflight and fixture.* moved to the gate layer (real-stack harness self-tests)._
  - Lifecycle, control, fixtures, era (merged restarts), comp, core, lane (rules as unit tests, real stacks as gate), report, build, firstlaunch, release, signing, power, re, public, repo, setup.
  - Layer tags and `covers`; `lifecycle.isolated` narrowed to our pids.
- [x] 3.9 Dungeons, after m4-dungeon-scaling merges (≈ 1 h).
  - `DungeonsLogic` with its C++ tests.
  - Its checks placed in layers with `covers`.

- [ ] 3.10 Part 3 (design §15): close the gap.
  - Selector and runner: gate checks never run in pr (listed as owed); `harness/active-changes` no-impact; `report.gate_rule`, `report.selection` and `report.journeys` updated to the new spec text.
  - No cold `./play` or client session in the integration layer: backups kill-safe/restore on the shared server, play S1–S3 merged, client area.
  - Merged journey sessions (`first_play`, `saves`, `app_desktop`) within their budgets.
  - Saves and dungeons trims; `PENDING_SLEEPS`.
  - Measure: unit layer, one run of all integration + journey checks, the PR replay; targets in design §15.
  - _Part 3 (2026-10-11), design 15.1: selector/runner done; cold sessions merged (play + backups 896 → 504 s); first_play and app_desktop merged (229 → 127, 454 → 327 s). Measured: unit 38.2 s (pass), integration 1813 s (budget 900, FAIL), journeys 943 s (FAIL: app_desktop, saves, login_recovery), acceptance 2756 s (FAIL), replay 22.3 min (FAIL). Not done: on_quit, saves/dungeons trims, PENDING_SLEEPS, saves and login_recovery merges. Left unticked: the targets are missed._

## 4. Rules and docs
- [x] 4.1 CLAUDE.md §4, §6, §9 and §10 with the proposal's exact text (orchestrator; Zach approved the change).
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
