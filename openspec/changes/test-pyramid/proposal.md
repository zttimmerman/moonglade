## Why
Test time grows with every feature. The full run went from about 36 to 82 minutes in three days, and pause and saves each added about 9 minutes of real-client sessions and real-time waits. Every change pays the whole bill: builders run the quick tier per task, and the verifier runs one full run (82 min) per cycle, plus repeats. Earlier speed-ups gave 4–6 %.

Zach's decision (2026-10-10) is to restructure the tests the way a normal app's tests are built: a pyramid.

- **Unit tests** run on every commit or task. They cover:
  - Python harness and launcher logic;
  - pure logic extracted from the C++ module (MoongladeSaves, Pause, Dungeons rules);
  - the Lua add-on and login-screen code, run under a stub Lua runtime.
- **Acceptance tests** run only before a PR merges:
  - **integration tests:** the server probe against one shared server start, selected by the paths the change touches;
  - **journeys:** a small fixed set of real-client end-to-end runs, one at a time.
- **The complete suite** runs only at milestone gates and when shared core, build or patch code changes.
- **No nightly runs.**

## Baseline (measured)
The source is the last full run: 2026-10-10 13:08–14:30 UTC, repo `6c24409`, one lane (lane B fell back: 4.64 GB free < 5.0 GB). It recorded 236 records: 203 pass, 33 skips (the slow tier, by design).
- Wall time: 4935.6 s (82.3 min). The checks themselves took 4912.2 s.
- Building server binaries took 263 s.

**Where the time goes (by kind):**
| Kind | Checks | Time |
|---|---:|---:|
| `full` tier, stack down: each check starts its own `./play` session (own server start, often a real client) | 68 | 3072.6 s (51.2 min) |
| `full` tier, shared stack up | 42 | 1399.6 s (23.3 min) |
| `full` tier, stack-agnostic | 9 | 102.9 s (1.7 min) |
| `quick` tier (all of it) | 77 | 180.5 s (3.0 min) |
| Post-run reports, mostly `report.tiers`' nested quick run (155.9 s) | 7 | 156.6 s (2.6 min) |
| `slow` tier (skipped) | 33 | 0 |

**By area (prefix of the check id), largest first:**
| Area | Checks | Minutes |
|---|---:|---:|
| backups | 7 | 9.4 |
| saves | 21 | 9.3 |
| client | 22 | 9.1 |
| pause | 11 | 8.8 |
| app | 20 | 8.6 |
| login | 12 | 7.1 |
| play | 11 | 6.1 |
| build (incl. 246 s `build.clean`) | 6 | 4.9 |
| lane | 8 | 3.8 |
| report | 16 | 3.7 |
| savesctl | 10 | 2.9 |
| era | 8 | 1.9 |
| comp | 4 | 1.9 |
| lifecycle | 6 | 1.3 |
| core | 4 | 1.2 |
| firstlaunch | 7 | 0.9 |
| everything else (re, release, power, setup, harness, repo, fixture, public, signing, gm, sql, pop, perf) | 63 | 1.2 |
| **Total** | **236** | **81.9** |

What drives the time is structure, not slow code:
- About 50 separate server starts. A cold `hs up` takes 24.3 s, and a worldserver restart takes 14–16 s (`ready_s` in the evidence).
- About 12 separate real-client sessions of 70–170 s each.
- One pause or wait per scenario. For example, 5 pause scenarios each pause the world for their own 20 s.
- A 156 s nested quick run that re-proves what the run already ran.

## Targets (Zach, 2026-10-10)
| Layer | Target |
|---|---|
| Unit layer (all of it, even with hundreds of tests) | ≤ 1–2 min. Enforced at 120 s, aiming for ≤ 60 s |
| Per-PR acceptance run (integration + journeys selected for the change) | averages ≈ 15 min. Enforced: a rolling average of the last 10 PR runs ≤ 15 min (a miss is a FAIL; the PR may merge only with a STATUS.md process item and a named remedy) |
| Complete integration + journey suite | ≤ 30 min |
| A new feature's cost to a PR run | ≤ 2 min of new acceptance time |
| The selection step itself | ≤ 10 s |

Zach accepted these fixed costs:
- a server build when C++ changes (about 4 min from empty, less when incremental). It is reported separately and excluded from the budgets.
- the server start, and journeys running one at a time because there is one screen. Both are inside the budgets above.

Every layer and the selection step are timed in every run. A missed budget is a **failure** of the run, not a warning.

## What changes
- **Layers and tiers.** Every check declares a **layer**:
  - `unit`: no server, no client, no Wine;
  - `integration`: server probe, SQL, CLI, stand-in `./play`;
  - `journey`: the real client or the app, end to end;
  - `gate`: builds from empty, soaks, packaging, and real-stack harness self-tests.

  `hs run --tier unit|pr|full` replaces `quick|full|slow|gui|backups`:
  - `unit`: the unit layer only. It is the builders' loop, and `hs test` is the same thing.
  - `pr` (the merge requirement): unit + the integration tests and journeys the change touches. When the change touches a FULL-trigger path, an unknown path, or git can't answer, the selection becomes the complete acceptance suite.
  - `full`: everything, including all gate checks. It runs at milestone gates and is the sweep that is run three times at a gate.
- **Unit runners.** No Homebrew installs:
  - Python: stdlib `unittest`. pytest isn't installed anywhere, including `env/tools/venv`.
  - C++: a host-built test binary compiled with the system `clang++` (Apple clang 16) from logic extracted into `modules/mod-moonglade/src/logic/`, which uses only the standard library and zlib and links no server.
  - Lua: **Lua 5.1.5**, the client's Lua version, pinned in `fork.lock` like the other downloads and built with the system `cc` into `env/tools/`. Homebrew's `lua` is 5.4, so it is the wrong language version and a Homebrew dependency. It is not used.
- **One shared server for integration tests.** Each test gets fresh, uniquely named characters. A cheap state check runs between tests (world running, default profile, intervals restored). A leak fails the test that caused it and restarts the server. Config-profile tests are grouped, with one restart per profile. Tests that touch only their own characters run two at a time.
- **Short or controllable waits.**
  - Fixed sleeps become event polling.
  - Timers move into unit tests with injected clocks: Lua `OnUpdate` elapsed, and C++ functions that take `now`.
  - Real-time waits that the specs require (the 20 s pause, the 20 s logout countdown, the 30 s stay) are kept, but consolidated so several scenarios share one wait.
  - A static lint fails acceptance code that sleeps more than 1 s without a recorded, spec-cited reason.
- **Impact selection** maps changed paths to tests:
  - Each test declares the non-Python paths it asserts on (`covers`: C++, SQL, conf, Lua/XML, launcher, patches).
  - The Python import closure (`runner.import_closure`) gives the Python side.
  - A path covered by no test and not on the no-impact list (docs, specs) selects the complete suite, so selection is fail-safe.
  - Journeys are selected when they assert on the path. `journey.first_play` is the smoke journey for anything any journey merely runs through.
- **A fixed, named journey set** (6 now, at most 8; adding one needs a spec change). The six are below; each runs one at a time with its own time budget:
  - `journey.first_play`
  - `journey.pause`
  - `journey.saves`
  - `journey.login_recovery`
  - `journey.stock_client`
  - `journey.app_desktop` (GUI, stand-in client)
- **Warm journeys.** A harness-only switch lets `./play` leave a running stack up. It is refused outside a harness run and for the player's account. `journey.first_play` always runs the real cold start and the real shutdown.
- **Removed:**
  - the backups tier and the GUI tier as run tiers (impact selection replaces both; GUI-marked journeys still go through the GUI preflight);
  - the slow tier (it becomes the gate layer);
  - `report.tiers`' nested run;
  - `hs nightly` and its LaunchAgent (no nightly runs).

## Where each existing area goes
design.md, appendix A, has the full per-check table for all 236 records. In brief:

| Area (today) | Goes to | Why |
|---|---|---|
| **backups** (9.4 min) | **Unit:** rotation, manifest and refusal reasons, the failure notice text. **Integration:** one stand-in session for backup-on-quit ordering and one for an injected failure (cold); kill-safety and restore driven directly against the shared database into scratch schemas. **Journey:** the app's Backups window, in `journey.app_desktop` | Rotation and refusals are file logic and don't need a server. Kill-safety needs the database, not a game session. Only "backup after the world stops, before the database stops" needs a real `./play` quit |
| **saves** (9.3 min) | **Unit (C++):** labels, retention (5 autos, 1 quick, 3 before-load), the list line format, refusal reasons, the in-combat/dead predicate, JSON/manifest. **Integration:** roundtrip, scope, fresh item ids, in-world load, ordering, atomic, autosave triggers, bots skip; character-scoped tests two at a time. **Journey:** character-screen load and list | The rules are pure functions inside MoongladeSaves.cpp. What only the server can prove (database content after a load) stays integration |
| **client** (9.1 min) | **Unit:** config generation, the pristine lock, play-client build/revert, the resolution decision with injected displays, the config gate predicate. **Integration:** one warm stand-in run for the config gate (also feeds the not-rebuilt guard), protocol and realm smoke. **Journeys:** `stock_client` (stock exe); `first_play` (patched exe, first-start settings, written resolution). **Dropped:** `client.launch_smoke` | Every journey launches the real client, so a separate 38 s "does Wine start" smoke is redundant |
| **pause** (8.8 min) | **Unit (C++):** the pause state machine (who paused, refusals, unpause on logout or disconnect, cooldown arithmetic after resume). **Integration:** one 20 s pause checks combat, auras, cooldowns, cast cancel and bots together (`pause.freeze_matrix`, 5 checks → 1); respawn on a scratch spawn with a short respawn time; taxi refusal, logout and disconnect with per-test characters (no 90 s offline wait); the disabled profile in the grouped restart. **Journey:** `journey.pause`. **Gate:** the 5-minute session soak | Today 5 scenarios each pay their own 20 s pause plus setup |
| **app** (8.6 min) | **Unit:** icon geometry, bundle build in a sandbox, icon everywhere. **Integration:** startup failure (cold), warm loading stages. **Journeys:** `first_play` (open, loading stages, shutdown window); `app_desktop` (Dock identity, focus, reopen, second open, relocated copy, Backups window) | One app session covers what about 8 checks open separately today |
| **login** (7.1 min) | **Unit (Lua):** front-menu logic: no login before Continue (60 s on a simulated clock), the backdrop choice stored and reapplied, a forward-only overlay bar. **Integration:** the Continue context line. **Journeys:** `first_play` (fresh menu, Continue, overlay, character-select layout and wording, add-on report); `login_recovery` (friendly error, try again, reload, 10 s live no-login watch) | The glue's logic runs under a stub. Real rendering and the real login stay in journeys |
| **play** (6.1 min) | **Unit:** lock reclaim, signal handler mapping (SIGTERM, repeated signals), player-file rules. **Integration (cold):** S1 is one offline-sandboxed stand-in session covering normal exit, single instance, stage lines, world progress and Dock identity (6 checks → 1 session); S2 SIGINT; S3 client crash; startup failure; first-run account | These test the real start and stop, so they stay cold, but 6 sessions become 1 |
| **savesctl** (2.9 min) | **Unit:** the listener with a fake socket, the load verb, build refusal. **Integration:** switch refusals, warm hygiene run. **Journey:** the live control round trip, codes, listener down, realm restored and account name kept, all in `journey.saves` | The protocol is pure. The live client half needs the client |
| **build, core** (6.1 min) | **Unit:** patches apply on a scratch index. **Integration:** the login/logout race. **Gate:** build from empty, pin enforcement, smart clean, moved repo, the unpatched-baseline race | Selected when build inputs or core patches change, as today's `--force-clean` rule |
| **lane** (3.8 min) | **Unit:** paths, table, refusals, preflight rules, merge (stub checks). **Gate:** the 4 real two-stack tests, selected by the lane paths | They test the harness, not the game |
| **report** (3.7 min) | **Unit:** synthetic rule checks. **Run guards:** coverage, schema, timing. **Dropped:** `report.tiers`' nested quick run (156 s); the run's own unit-layer record is what it re-measured | Duplicate work |
| **era, comp, lifecycle, fixtures, gm, sql** (5.2 min) | **Integration** on the shared server. `era.toggle_roundtrip` and `era.scope_guard` share their off/on restarts (4 → 2). `lifecycle.isolated` checks only our processes (18.6 s → ~3 s) | Already server-probe tests; they just stop paying their own starts |
| **firstlaunch, release, signing, setup, public, power, re, repo** (2.6 min + 33 slow skips) | **Unit:** static and preflight checks. **Gate:** the former slow tier (packaging, first-launch flows, signing, population/perf soaks) plus loopback downloads and Ghidra, selected by their paths | Release tooling changes rarely, and these are slow by nature |

Totals across the 236 records:
| Destination | Records |
|---|---:|
| Unit | 62 |
| Integration | 74 (65 on the shared server + 9 cold) |
| Journeys | 34 (15 journey sessions today become 6) |
| Merged into another test | 10 |
| Gate | 44 |
| Run guards | 10 |
| Dropped | 2 |

## Projected result (arithmetic in design.md §9)
| Layer | Today | Projected |
|---|---:|---:|
| Unit layer (builders, every task) | quick tier 180 s with a server start | **≈ 46 s** warm, ≈ 76 s when the C++ test binary rebuilds |
| Integration, complete | 2308.7 s (38.5 min) across today's checks | **1200.7 s (20.0 min)**, with 2 workers for character-scoped tests; 1368.4 s (22.8 min) serial |
| Journeys, complete | 1437.5 s (24.0 min) in about 15 sessions | **575 s (9.6 min)** in 6 |
| **Complete acceptance suite** | 3746.2 s (62.4 min) | **1775.7 s (29.6 min)** (32.4 min if the 2-worker pool proves unsafe; see Risks) |
| Gate extras (only in `full` or when selected) | 605.5 s + 33 slow checks | unchanged, timed separately |
| Typical feature PR, incl. unit layer, excl. build | 82 min full run | **5.6–13.1 min** (worked examples in design.md §9.4) |
| FULL-trigger PR (core patch, build inputs, stack, probe, fork.lock) | 82 min | **30.4 min** + build |

**Per-PR average: a projected miss on the recent mix.** I replayed the 15 merged PRs #19–#33 by the paths each one touched (`git diff M^1 M`), using the selection rules (design.md §8):
- **9 of the 15 touched FULL-trigger paths.** `stack.py`, `build.py`, `fork.lock`, the probe library, core patches, and `util.py`, which every test imports.
- **The projected mean is 22.2 min, a miss.**
- The 6 non-FULL PRs average 9.9 min.
- The 15 min mean holds only while at most 25 % of PRs are FULL triggers: (15 − 9.9) / (30.4 − 9.9).

That stretch was mostly platform work: setup, release, lanes, power and the test loop. The levers are in design.md §9.5. Lane B running integration beside the journeys brings the replay to about 16.8 min, but only when memory allows. Narrowing what a FULL-trigger PR runs brings it to about 18.6 min (Open question 1). No lever alone reaches 15 min on that mix. Feature-heavy work does.

Task 1.3 measures this with the real selector before anything is migrated. From then on, `report.pr_average` reports every drift as a FAIL.

## CLAUDE.md rule changes (Zach approved changing these; exact proposed text)

**§4, third bullet, replaced by:**
> - **Every scenario maps to an automated check at the lowest layer that can prove it** (unit, integration, or one of the fixed real-client journeys; see `harness-reporting`), or is explicitly tagged `[human-verify]` with a reason. A scenario with neither is incomplete, and the change cannot be approved. Adding a journey or growing the journey set is a spec change reviewed at the next human gate.

**§6, the builder and verifier bullets, replaced by:**
> - **Builder subagents:** implement one task against one spec, with a clean context per task. Builders run the unit layer (`harness/hs test`) plus the one acceptance test they are writing, repeated as needed. They never run an acceptance tier per task.
> - **Verifier subagent:** a fresh context that did not build the change. It receives only the spec deltas, the diff, and harness access. It runs the PR tier once (`harness/hs run --tier pr`: the unit layer, then the integration tests and journeys the change touches, or the complete suite when the selector says FULL), repeats each new or changed test 3×, and returns PASS or FAIL per scenario with evidence. A missed layer budget is a FAIL.

**§9, steps 3–6, replaced by:**
> 3. Delegate to a builder with the spec deltas and task. The builder's loop is the unit layer plus the test it is writing.
> 4. Send the result to the verifier (one PR-tier run).
> 5. FAIL: return to the builder with the verifier's evidence. Reproduce the failure at the cheapest layer that shows it (a unit test, else the single failing test) before rerunning anything larger. After 3 failed cycles on one task, mark it blocked with a written diagnosis and move to another task. Do not stop the session.
> 6. PASS: integrate. The PR tier is the merge requirement. Its selection runs the complete suite by itself when shared core, build or patch paths change or a path is unknown. Update STATUS.md and archive the change if all its tasks are done. At each milestone gate, run the complete suite (`--tier full`) three times; any failure in any run is P0.

**§10, the third bullet, replaced by:**
> - Passing a hand-picked subset of the harness. (The PR tier's automatic selection is not hand-picked: it is the merge requirement when its selection, its time budgets and the run all pass. A missed time budget is a failure, not a note.)

The `hs-change-loop` and `hs-harness-checks` skills get the matching run budget. The builder budget becomes unit + own test. The verifier budget becomes one PR-tier run + 3× new/changed tests + 2–3 counterexample spot checks. The "full ×3 weekly" sweep moves to milestone gates only.

## Risks
- **The selector or the shared server makes runs slower or less safe.**
  - A broad import closure (`play.py` reaches most of the harness) could select almost everything, or a narrow map could miss a regression. Mitigations:
    - selection time is budgeted (≤ 10 s) and recorded in every run;
    - task 1.3 replays real merged PRs before anything is migrated;
    - unknown paths select FULL;
    - journeys exist to catch cross-feature breakage;
    - the milestone-gate full ×3 is the backstop.
  - A shared server can leak state between tests (a paused world, a changed interval, a lingering session) and create order-dependent flakes. Mitigations:
    - a reset check between tests, with a budget (median ≤ 1 s, max ≤ 3 s);
    - a leak fails the test that caused it and restarts the server;
    - fresh characters per test;
    - task 1.2 runs the prototype slice 3× and with the order reversed.
- **The 30 min complete-suite target depends on the 2-worker pool** for character-scoped integration tests (167.7 s saved). If the pool proves flaky, the serial projection is 32.4 min, a miss. These are the fallback levers, each measured in task 1.2:
  - a passive 30 s observer for `client.login_text_clean` (−30 s);
  - one shared scratch play client for the warm journeys (−20 s);
  - `lifecycle.persist` folded into the era restarts (−25 s);
  - lane B running integration while lane A runs journeys, when memory allows (about 21 min, but the last run fell back for memory).
- **The 15 min PR average depends on the mix of PRs**, and on the recent mix it is a projected miss (22.2 min; see Projected result and Open question 1).
- **Escaped cross-feature regressions** are found at the milestone gate instead of in the PR that caused them. This is the accepted trade-off of the pyramid. The gate's failures are bisected to a PR, and each escape adds a `covers` entry or a journey assertion.
- **The extracted C++ logic drifts from the module.** The module itself calls the extracted functions (no copies), and the module build compiles `logic/`. Task 1.1 verifies that AzerothCore's module source collection picks up the subfolder.
- **Lua stub fidelity.** A stub that's too kind hides bugs. The stubs implement only the client API the glue and add-on call; each stub function's behaviour is recorded with its source (FrameXML/GlueXML from the installed client's base archives). The journeys still run the real client.
- **Warm-stack switch leakage.** It is a new test switch, so it follows checklist item 1: refused outside a harness run and for the player's account, with a refusal check.
- **Big change.** About 230 checks move. Each migration slice moves one area, measures that area before and after, keeps or tightens every scenario, and re-points its `Check:` lines in the same commit.

## Open questions for Zach
1. **What a FULL-trigger PR runs.** You said the complete suite runs when shared core, build or patch code changes. On the recent mix that makes 9 of 15 PRs FULL, and the per-PR mean 22.2 min instead of 15. There are three choices; the proposal implements (a) and lists (b) and (c):
   - (a) **Keep it as you said.** The complete suite (29.6 min) runs, and the 15 min mean is met only in feature-heavy stretches. `report.pr_average` shows the miss as a FAIL in platform-heavy ones.
   - (b) **Narrow it.** A FULL-trigger PR runs the complete integration layer plus `first_play` and the journeys that assert on its paths. All six journeys run only at milestone gates. FULL becomes about 24.3 min, and the replay mean about 18.6 min.
   - (c) **Also use lane B** when memory allows. That gives about 21.3 min and a mean of about 16.8 min.

   One non-lever: harness-engine changes (`runner.py`, `registry.py`) are not FULL triggers in the proposal (the unit layer covers the runner's rules, plus a FULL selection dry-run and the server smoke), but no replayed PR was engine-only, so this doesn't move the mean.

   **Smaller follow-ups:**
   - `config.py`/`paths.py` changes that only add or change constants are selected by name instead of FULL (spec rule 4).
   - `patches/client/**` is mapped to the client tests and journeys, not FULL.
2. **The 60 s "no login before Continue" observation** (`login.cancel_shows_list`) becomes a 60 s simulated-clock unit test plus a 10 s live watch in `journey.login_recovery`. This weakens the live part, so it is logged below for the gate.
3. **`hs nightly`** is removed. If you installed its LaunchAgent, run `harness/hs nightly --uninstall` once before this merges. Agents can't touch LaunchAgents.

## Sources
- Baseline: the full run 2026-10-10 13:08 UTC (`6c24409`), per-check `duration_s` and evidence. The by-area and by-kind numbers are computed from it in design.md §1.
- The registry's tiers, stacks and markers come from the check registry at `699d778` (origin/main), loaded read-only.
- PR replay: the merge commits of #19–#33 on `origin/main`, `git diff --name-only M^1 M`.
- No game data values change.

## Spec-change log
- 2026-10-10 (structure, Zach's decision): the tiers `quick|full|slow` plus the run tiers `gui|backups` become layers `unit|integration|journey|gate` and run tiers `unit|pr|full`.
  - "Single-command full run" is modified.
  - "Test tiers" is removed and replaced by "Test layers and run tiers" (OpenSpec can't drop a scenario through MODIFIED).
  - "Slow-tier scenario checks", "GUI tier", "GUI path rule covers shared helpers", "Backups tier" and "Nightly flake hunt" are removed and replaced (see the deltas' Reason/Migration).
  - The quick tier's 3 min budget becomes the unit layer's 2 min budget. This is a tightening.
- 2026-10-10 (verification move, for review at the gate): the 60 s live "no login before Continue" observation becomes a simulated 60 s unit test plus a 10 s live watch (Open question 2). This is a loosening of the live part.
- 2026-10-11 (orchestrator, after part 2; design §15): gate checks never run in a `pr` run. They run in full runs, and a pr run lists the ones its paths select as owed. A change whose deltas add or change a gate check runs it alone before merge. This replaces "selected gates run in pr"; gate checks also run at every milestone gate and release tag, so this narrows when they run, not what must pass. **For review at the gate.**
- 2026-10-11 (correction): the journey cap goes from 12 to 14 min. It contradicted the six journeys' own budgets (810 s), which are unchanged. `harness/active-changes` joins the no-impact list.
- 2026-10-11 (clarification): `journey.first_play` is described as the `./play` first play that was built; the app's cold open, Dock and quit are `journey.app_desktop`'s, as built. `client.launch_smoke` stays, because its client-connectivity scenario still names it.
- 2026-10-11 (part 2, builder; for review at the gate): `lane.merge`, `lane.preflight` and `fixture.*` move to the gate layer (real-stack harness self-tests), so they run in full runs only. Their rules are still covered by unit tests.
- Migration slices will add their own entries here: one per capability whose `Check:` lines are re-pointed, scenario text unchanged unless the entry says otherwise.

## Measurement log
- 2026-10-11 (tasks 1.1–1.4, prototype; numbers in design.md 12.1): **no-go as designed.** Unit layer projected ≈ 47 s warm with the 4-process pool (each unit test ≤ 0.06 s; C++ build 1.4 s, Lua build 2.4 s): pass. Shared-server slice (4 saves tests, 7 runs): statuses identical, reset median 0.06 s / max 0.07 s, pool −36.6 %, but per-test identity costs 4.6 s, so 3 of 4 tests exceed their projection + 20 %: fail; the pause ports were not done. Replay of #19–#33: selection ≤ 0.18 s, but the import closures select ~all tests for any harness change, and the complete suite is 38.2 min with the m4-dungeon-scaling checks; mean 37.1 min (strict FULL) and 33.3 min (option 2), against the 15 min target: a miss.
- 2026-10-11 (tasks 1.5–1.7, after the design 13 redesign; numbers in design.md 13.1): ownership selection replays #19–#33 + #1–#3 with 22 % FULL; mean 13.5 min strict / 12.5 min option 2 at the measured clustering ratio (21.0 / 20.0 with no clustering gain); selection ≤ 0.09 s. Saves cluster 9.8 s median; `dng.session` 142 s against its 60 s budget (miss); run-start fixtures 14.3 s against 10 s (miss: the world-facts query); first round unstable (a 14 s query timing out under the pool), stable 7 of 7 after caching it. Projected complete suite 23–25 min (pass); integration layer 14.6 min against 12 min (miss).
- 2026-10-11 (test method, design §14): `scale.pause_safe` pauses 5 s instead of 20 s. Scaled values are compared exactly and the pause offset to the millisecond, so a 5 s pause shows any drift as well as 20 s did; it saves ~15 s in `dng.session`. Review at the M4 gate.
- 2026-10-11 (correction, builder found it): the selection requirement still described the 1.3 import-closure selector; rewritten to design §13 (declared ownership, narrow FULL list, smoke set for unowned/shared paths, config by section). New requirement "Every product path has an owner" (`report.ownership`). `report.layer_budgets` also enforces the 15-min integration budget (design §14). Clustered sessions register each scenario's existing check id as a child record, so `Check:` lines are unchanged.
- 2026-10-11 (part 1: engine 2.x, slices pause, saves + savesctl, dungeons, backups unit; numbers in design.md 12.2): part-1 integration 1317.8 s before → 800.5 s after (pause 532.9 → 136.6, saves + savesctl 435.1 → 392.4, dungeons 349.9 → 167.8); `journey.pause` warm 73.7 s (budget 90); `dng.session1/2` 83.1 / 65.2 s (budget 90 each); unit layer 38.5–49.9 s; fixture start 4.7 s; resets ≤ 0.112 s; no leaks. Saves gained least (clustering groups the scenarios but each keeps its work). Spec text unchanged; every `Check:` id is unchanged (clustered sessions record the scenario ids as children).
- 2026-10-11 (part 2: journeys, fixes, part-2 slices; numbers in design.md 12.3): **budgets missed.** Full run at `3c95011` (one lane, lane B refused for memory): unit layer 39.3 s (pass; the `hs test` tier 50.5 s wall); integration 2567.3 → 2321.4 s (budget 900); journeys 1526.0 → 1175.4 s; acceptance 4093.3 → 3496.8 s = 58.3 min (budget 30 min); gate 5328.6 s timed apart. Journeys: pause 76.4/90, stock_client 32.1/60 pass; login_recovery 151.2/120, saves 194.6/150, first_play 229.1/150, app_desktop 453.5/240 miss. Fixture resets median 0.066 s, max 0.112 s, no leaks. The pr tier on this branch selects every acceptance test and every gate check (51) without FULL, because the branch edits every check file.
