## MODIFIED Requirements

### Requirement: Single-command full run
`harness/hs run` SHALL run the checks of the selected run tier (`unit`, `pr` or `full`; default `pr`), optionally filtered by `--only <glob>`, bring the stack up as needed, and exit 0 only if every executed check passed and every time budget of the run held. On a run that isn't full (filtered, tier `unit`, or tier `pr` whose selection isn't FULL), `report.full_run` records `skip` ("not a full run") instead of `fail`, and that skip doesn't affect the exit code. A run counts as **full** only if it is unfiltered and its tier is `full`, and it SHALL include every check and every unit test referenced by any scenario in the archived specs and in the active changes (those listed in `harness/active-changes`). A `pr` run whose selection is FULL SHALL execute every unit, integration and journey check (the complete acceptance suite) and no gate check.

#### Scenario: Full run produces reports
- **GIVEN** a built server
- **WHEN** `harness/hs run --tier full` runs
- **THEN** `harness/results.json` and `harness/summary.md` are written, and the exit code is 0 iff every check's status is `pass` and every layer budget held (`report.full_run` may only be `skip` on a non-full run, and must be `pass` on a full run)
- Check: `report.full_run`

#### Scenario: Every spec scenario maps to a check
- **GIVEN** the specs under `openspec/specs/` and the spec deltas of every change listed in `harness/active-changes`
- **WHEN** the harness runs its coverage self-check
- **THEN** every scenario has either a `Check: <id>` naming a registered check or a unit test the unit layer lists (`unit:py:<module.Class.method>`, `unit:cpp:<suite.name>`, `unit:lua:<file.name>`), or a `[human-verify]` tag with a reason; any other scenario makes this check fail and is listed
- **AND** on a full run every referenced unit test passed in this run's unit layer
- Check: `report.coverage`

### Requirement: Full-run timing is recorded and guarded
The harness SHALL record per-check timing for every run, and per-layer totals (unit, selection, integration, journeys, gate, build). An unfiltered full run SHALL fail the timing guard for a real slowdown: the checks recorded in the per-check timing baseline, taken together, SHALL NOT take more than 10% longer than their baseline times on the same machine, with no baseline check removed or weakened. Time spent building server binaries (`build.clean`, and any baseline-server rebuild inside a check) SHALL be reported separately and excluded from that comparison. Checks not in the baseline SHALL be reported with their times; any single such check taking more than 180 s SHALL be listed as a warning in the summary. A check joins the baseline, with its time from the merging change's verifier run, when that change merges; existing baseline times are never raised that way. When a migration replaces checks, the replaced checks leave the baseline in the same commit and their replacements join it from that slice's measured run, with the before/after area totals recorded in the proposal.

#### Scenario: Timing summary and no regression
- **GIVEN** a per-check timing baseline recorded in the repo (each check's time, with the run it came from)
- **WHEN** an unfiltered full run completes
- **THEN** results.json has a timing summary (per check, per layer, total, top 10, build time, new checks and their times, warnings); every baseline check that the run's layers execute ran; and the sum of the executed baseline checks' times, minus build time, is ≤110% of the same checks' baseline sum
- Check: `report.timing`

#### Scenario: Slowdowns fail, new checks and rebuilds don't
- **GIVEN** synthetic timing records against a known baseline
- **WHEN** (a) the baseline checks are 15% slower in total, (b) they're unchanged but 300 s of new checks are added, (c) they're unchanged but a from-empty build adds 400 s, (d) a single new check takes 200 s
- **THEN** (a) fails; (b) and (c) pass the per-check guard with the new time and build time reported; (d) passes it with a warning naming the check (the layer budgets judge (b) and (d) separately)
- Check: `report.timing`

### Requirement: The GUI preflight only guards runs that need the screen
An unfiltered `pr` or `full` run SHALL refuse to start because of a locked screen or a sleeping display only when its selection includes a GUI-marked check (today `journey.app_desktop` and gate checks marked gui). Low battery while on battery power SHALL always refuse a `pr` or `full` run. `--ignore-gui-preflight` overrides both. `unit` runs are never refused for the screen.

#### Scenario: Locked screen, no GUI checks due
- GIVEN a checkout whose selection includes no GUI-marked check, and a probe reporting the screen locked
- WHEN an unfiltered pr run starts
- THEN the run is not refused, and the screen state is recorded in the run's evidence
- Check: `report.gui_preflight`

#### Scenario: Locked screen, GUI checks due
- GIVEN a checkout whose selection includes a GUI-marked check (or a full run), and a probe reporting the screen locked or the display asleep
- WHEN the run starts
- THEN it exits 2 within 5 seconds with a one-line reason, before any check runs
- AND on battery power below 35% it refuses whatever the selection says
- Check: `report.gui_preflight`

### Requirement: The last full run's results are kept
Every unfiltered full run SHALL write its results.json and summary.md to `harness/out/last-full-run/`. Unit, pr and filtered runs SHALL never overwrite them. `hs timing-baseline join` SHALL default to that copy.

#### Scenario: A later filtered run doesn't overwrite the full run
- GIVEN an unfiltered full run has completed
- WHEN a filtered run (`--only`) and a unit run complete afterwards
- THEN `harness/out/last-full-run/results.json` still holds the full run (`full_run: true`, its repo sha and start time)
- AND `hs timing-baseline join --label x` without a results path reads that copy
- Check: `report.last_full_run`

## REMOVED Requirements

### Requirement: Test tiers
**Reason**: Zach's test-pyramid decision (2026-10-10): check tiers `quick|full|slow` become layers `unit|integration|journey|gate`, and run tiers become `unit|pr|full`; the quick tier's 3 min budget becomes the unit layer's 2 min budget.
**Migration**: Replaced by "Test layers and run tiers" below. "Quick tier runs fast and says so" (`report.tiers`, a nested quick run) becomes "Unit tier runs fast, starts nothing and says so" (`report.unit_layer`, read from the run's own unit-layer record); "Every check has a tier" (`report.tiers_declared`) becomes "Every check has a layer" (`report.layers_declared`).

### Requirement: Slow-tier scenario checks
**Reason**: Zach's test-pyramid decision (2026-10-10): the slow tier becomes the `gate` layer, which runs in every full run and in a pr run when its paths change.
**Migration**: Replaced by "Gate-layer checks" below. Every slow check becomes a gate check; `report.slow_tier_rule` is replaced by `report.gate_rule`.

### Requirement: GUI tier
**Reason**: GUI-marked checks collapse into one journey (`journey.app_desktop`) selected by the impact map like every other test; a separate GUI run tier and its own path rule are no longer needed.
**Migration**: Replaced by "Impact selection is fail-safe" and "Fixed journey set". The GUI marker stays for the preflight ("The GUI preflight only guards runs that need the screen", modified above). `report.gui_tier_rule` is replaced by `report.selection`.

### Requirement: GUI path rule covers shared helpers
**Reason**: Impact selection applies the import closure to every test, not only to the GUI checks.
**Migration**: Replaced by "Impact selection is fail-safe", whose scenario includes a shared helper imported by a journey.

### Requirement: Backups tier
**Reason**: The backups checks become unit tests, integration tests and part of `journey.app_desktop`, selected by the impact map like everything else; a fixed backup path list is no longer needed.
**Migration**: Replaced by "Impact selection is fail-safe". `report.backups_tier_rule` is replaced by `report.selection`. The run-level guard `backups.player_untouched` stays.

### Requirement: Nightly flake hunt
**Reason**: Zach (2026-10-10) rejected nightly runs as the answer to test time.
**Migration**: `hs nightly` and its LaunchAgent are removed (Zach uninstalls the agent if he installed it). Flake detection is the full ×3 at each milestone gate plus 3× repeats of each new or changed test per change. `report.nightly` is removed.

## ADDED Requirements

### Requirement: Test layers and run tiers
Every check SHALL declare exactly one **layer**: `unit` (no server, no client, no Wine process; a check that starts one fails), `integration` (server probe, SQL, CLI or a stand-in `./play`), `journey` (the real client or the app end to end; only the named set in "Fixed journey set") or `gate` (builds from empty, soaks, packaging, real-stack harness self-tests). Run tiers SHALL be: `--tier unit` runs the unit layer only (`harness/hs test` is the same run); `--tier pr` runs the unit layer, then the integration and journey checks the impact selection picks, and lists the gate checks it selects as owed without running them; `--tier full` runs every check of every layer. `results.json` SHALL record the tier, each record's layer, and the selection.

#### Scenario: Unit tier runs fast, starts nothing and says so
- **GIVEN** a checkout with the stack down
- **WHEN** `harness/hs run --tier unit` runs
- **THEN** it executes every unit-layer check and every unit test (Python, C++, Lua) and nothing else, no stack, Wine or client process is started (sampled during the run), it finishes in at most 120 s on the target Mac, records `tier: unit` and each unit test's id and duration, `report.full_run` reports `skip` ("not a full run"), and the command exits 0 iff every unit check and unit test passed and the budget held
- Check: `report.unit_layer`

#### Scenario: Every check has a layer
- **WHEN** the harness lists registered checks
- **THEN** each has exactly one layer from {unit, integration, journey, gate}; `build.clean` and `core.login_logout_race_baseline` are `gate`; no check outside the named journey set is `journey`
- Check: `report.layers_declared`

### Requirement: Unit layer
The unit layer SHALL run without Homebrew additions and without any server, client or Wine process: Python unit tests under `harness/tests/unit/` with the standard library's `unittest`; a C++ test binary built with the system `clang++` from `modules/mod-moonglade/src/logic/` (standard library and zlib only, no AzerothCore header) and the tests under `harness/tests/unit/cpp/`, rebuilt only when its inputs' hash changes; Lua tests under `harness/tests/unit/lua/` run by Lua 5.1.5 built from a download pinned by URL and SHA-256 in `fork.lock` against a stub of the client API the glue and add-on call; and the `unit`-layer checks. The module code SHALL call the extracted logic itself (no copies). A missing or unbuildable unit runner SHALL fail the layer, never skip it. Each unit test SHALL take at most 2 s; the C++ build (at most 60 s) and the Lua build are timed separately.

#### Scenario: All three runners report per-test results
- **GIVEN** a checkout with one known-passing and one deliberately failing test in each of the Python, C++ and Lua suites (a scratch copy of the test folders)
- **WHEN** the unit layer runs on it
- **THEN** results record each test's id, status and duration, the three failing ones fail the run with their messages, and the passing ones pass
- **AND** with the Lua runner's pinned archive hash altered, the Lua suite fails ("pinned download mismatch"), not skips
- Check: `report.unit_layer`

#### Scenario: The module uses the logic the unit tests test
- **GIVEN** the extracted logic sources
- **WHEN** the unit layer's source scan runs
- **THEN** every function the C++ unit tests call is defined once, under `src/logic/`, and called from the module sources; no file under `src/logic/` includes an AzerothCore header
- Check: `report.unit_layer`

### Requirement: Impact selection is fail-safe
A `pr` run SHALL select tests from the paths the change touches (`git diff --name-only --no-renames <merge-base with origin/main>` plus staged, unstaged and untracked files), using declared ownership, not import closures. Each integration, journey and gate test SHALL declare a `covers` list: product paths (module sources, add-on and glue files, launcher, scripts), the harness modules its check file imports directly, and module-config sections (for example `Moonglade.Saves.*`). Shared harness infrastructure (modules that six or more acceptance check files import directly, plus `probe/**`, `stack.py`, `lanes.py`, `lanestack.py`, `runner.py`) is owned by the unit layer and the smoke set, not by every test. A path SHALL select:
1. FULL (the complete acceptance suite) if it is `patches/core/**`, `patches/playerbots/**`, a core or playerbots pin key in `fork.lock`, `harness/hslib/build.py`, or a CMake file;
2. nothing beyond the unit layer if it is `openspec/**`, `docs/**`, `*.md`, `harness/tests/unit/**` or `harness/active-changes`;
3. otherwise every test whose `covers` names it (a module-config file selects by the sections whose keys changed);
4. the smoke set (`journey.first_play` plus the cheapest non-targeted integration test of each area) if no test covers it, if it is shared infrastructure, or if it is `mod_moonglade_loader.cpp`.

A **targeted** integration check ("Targeted integration checks") is selected only by rule 3, never by FULL or the smoke set. A git failure, an unparseable source, or a stale `covers` entry (matching no file) SHALL select FULL. The selection, the reason for each selected test and the selection's own duration SHALL be recorded.

#### Scenario: Unknown, docs, owned and shared paths
- **GIVEN** synthetic changed-path sets on a scratch git repo
- **WHEN** the selector runs for (a) `docs/x.md` only, (b) `modules/mod-moonglade/src/MoongladePause.cpp`, (c) a new file under `harness/hslib/` that no `covers` names, (d) `patches/core/0003-world-pause.patch`, (e) a git failure, (f) a helper that only `harness/hslib/checks/playclient_util.py` imports, (g) a changed `Moonglade.Saves.*` key in the mod-moonglade config, (h) a changed function body in `harness/hslib/stack.py`, (i) the `autobalance` pin key in `fork.lock`, and (j) the core pin key in `fork.lock`
- **THEN** (a) selects only the unit layer; (b) selects the pause tests, `journey.pause` and no saves test; (c) and (h) select the smoke set with the reason recorded; (d), (e) and (j) select FULL with the reason recorded; (f) selects only the tests that declare `playclient_util.py` or the helper; (g) selects the saves tests and no pause test; (i) selects the tests that declare mod-autobalance, not FULL
- **AND** the selection step took at most 10 s, recorded
- Check: `report.selection`

### Requirement: Every product path has an owner
Every tracked product path (module sources and configs, `client-mods/**`, `harness/app/**`, `play`, `bootstrap`, `tools/release/**`) SHALL be named by at least one test's `covers` or by the FULL or no-impact lists. A unit check SHALL fail and list any path with no owner, so new product code can't silently fall back to the smoke set.

#### Scenario: A new product file without an owner
- **GIVEN** the ownership map of this tree, and a synthetic tree that adds `modules/mod-moonglade/src/MoongladeNew.cpp`, named by no `covers`
- **WHEN** the ownership check runs
- **THEN** it passes on this tree and fails on the synthetic tree, listing `MoongladeNew.cpp`
- Check: `report.ownership`

### Requirement: Fixed journey set
The `journey` layer SHALL be exactly this named set, each with a time budget, run one at a time with no other check overlapping it: `journey.first_play` (150 s; cold `./play` → fresh play client → Continue → character select → world → quit → everything down, then the same session warm), `journey.pause` (90 s), `journey.saves` (200 s), `journey.login_recovery` (150 s), `journey.stock_client` (60 s), `journey.app_desktop` (240 s; GUI, stand-in client: cold app open, Dock, focus, quit, shutdown window). The set SHALL hold at most 8 journeys and at most 15 min (900 s) of budgets in total; adding, removing or re-budgeting a journey is a spec change.

#### Scenario: The registry matches the named set and journeys never overlap
- **GIVEN** the registry and a completed full run
- **WHEN** the journey rule runs
- **THEN** the registered journey-layer checks are exactly the six named ids with the budgets above, their budgets sum to at most 900 s, and the run's timeline shows no two journeys and no journey and another check overlapping
- **AND** a scratch registry with a seventh unnamed journey fails the rule
- Check: `report.journeys`

### Requirement: Every layer is timed and budgeted
Every run SHALL record the wall time of the unit layer, the selection step, the integration layer, each journey, the gate checks and build time, and SHALL fail (`report.layer_budgets`) when: the unit layer took more than 120 s; the selection took more than 10 s; a journey exceeded its budget; the integration layer of a full run, or of a pr run whose selection is FULL, took more than 15 min (pool time, targeted checks excluded); a full run, or a pr run whose selection is FULL, spent more than 30 min in integration and journeys (build time, gate checks and targeted checks excluded); the targeted checks of a full run took more than 20 min in total; or the checks of this run that aren't in the timing baseline and that the selection picked add more than 120 s (the "a new feature adds at most 2 min" rule). The summary SHALL show each layer's time against its budget.

#### Scenario: Each budget miss fails the run
- **GIVEN** synthetic run records
- **WHEN** (a) the unit layer took 121 s, (b) selection 11 s, (c) `journey.pause` 95 s, (d) a FULL-selection pr run 31 min of integration and journeys, (e) a full run 31 min of them with 10 min of build time on top, (f) a pr run with 130 s of new selected checks, (h) a full run with 16 min of integration, (i) a full run with 21 min of targeted checks, (j) a full run of 29 min plus 18 min of targeted checks, (g) a full run of 29 min plus 25 min of build and gate time
- **THEN** (a) through (f), (h) and (i) each fail naming the layer, its time and its budget; (g) and (j) pass with the build, gate and targeted time reported
- Check: `report.layer_budgets`

### Requirement: The per-PR acceptance average stays at or under 15 minutes
Every completed `pr` run (not stopped at the unit layer or by the power guard) SHALL append its integration-plus-journey time, whether its selection was FULL, its changed paths and the repo SHA to the run history (`harness/out/pr-runs.jsonl`). From the fifth recorded run on, a `pr` run SHALL fail `report.pr_average` when the mean of the last 10 recorded pr runs (this one included) exceeds 15 min; with fewer than 5 recorded runs it SHALL report, without failing, the mean of the selector's replay of the last 15 merged PRs (each PR's changed paths, per-check times from the last full run). The summary SHALL show the mean, the share of FULL selections and the slowest runs. A run whose only failure is `report.pr_average` MAY merge only after the miss is recorded in STATUS.md as a process item with a named remedy (CLAUDE.md §9); it is never reported as a pass.

#### Scenario: The rolling average fails when it drifts
- **GIVEN** a scratch run history of 9 pr runs averaging 14 min
- **WHEN** a 25 min pr run completes, and separately a 15 min one
- **THEN** the first fails `report.pr_average` (mean 15.1 min), naming the mean, the FULL share and the slowest runs; the second passes (mean 14.1 min)
- **AND** with 3 recorded runs it reports the replay's mean and the PRs it replayed with status pass and the note "fewer than 5 recorded runs"
- Check: `report.pr_average`

### Requirement: Targeted integration checks
An integration check SHALL be **targeted** only if it is on the reviewed list in design §16, each entry with its measured time and the reason it can't get cheaper (real waits the specs require, or a session of its own). A targeted check runs in every full run (milestone gates, release tags) and in a `pr` run only when a path its `covers` names changed; FULL selections and the smoke set never pick it. Adding a check to the list is a spec change.

#### Scenario: A targeted check runs when its own code changes, not on FULL
- **GIVEN** a targeted check whose `covers` names `harness/hslib/backups.py`
- **WHEN** the selector runs for (a) `harness/hslib/backups.py`, (b) `patches/core/0003-world-pause.patch` (FULL) and (c) an unowned new file (smoke set), and a full run executes
- **THEN** (a) selects it; (b) and (c) don't, and (b) and (c) record it as "targeted, not selected"; the full run runs it
- **AND** a registry with a targeted check missing from the design §16 list fails the rule
- Check: `report.targeted`

### Requirement: Gate-layer checks
Gate checks SHALL run in every full run (milestone gates, release tags) and never in a `pr` run. A `pr` run whose changed paths a gate check's `covers` names SHALL list that check in its summary under "gate checks owed" with the path that selected it; an owed check is neither run nor a failure. A change whose spec deltas add or change a gate check SHALL be verified with a run that executes that check (`--only <id>`) before it merges. Gate checks are excluded from the layer budgets and stay under the per-check timing guard.

#### Scenario: Gate checks run only in full runs, and pr runs list them as owed
- **GIVEN** a gate check whose `covers` names `harness/hslib/lanes.py`
- **WHEN** a pr run with only `docs/x.md` changed, a pr run with `harness/hslib/lanes.py` changed, and a full run execute
- **THEN** the first doesn't run it and records nothing for it; the second doesn't run it and lists it as owed, naming `harness/hslib/lanes.py`; the third runs it and requires it to pass
- Check: `report.gate_rule`

### Requirement: Acceptance tests don't sleep through fixed waits
Acceptance-layer code (integration and journey checks and the helpers they import) SHALL NOT wait with a fixed sleep longer than 1 s unless the wait is on a reviewed allowlist naming the spec that requires the real time (for example the 20 s pause, the 20 s logout countdown, the 30 s stay); other waits SHALL poll for the event with a timeout that only bounds them.

#### Scenario: A new long sleep fails the lint
- **GIVEN** the acceptance sources
- **WHEN** the lint scans them (calls to `time.sleep`, `wait_until` and the play-run waits with a literal or named constant over 1 s)
- **THEN** every such wait is on the allowlist with its spec citation
- **AND** a scratch copy with an added `time.sleep(5)` in a check fails, naming the file and line
- Check: `report.no_fixed_sleeps`
