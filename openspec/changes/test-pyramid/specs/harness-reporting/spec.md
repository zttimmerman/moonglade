## MODIFIED Requirements

### Requirement: Single-command full run
`harness/hs run` SHALL run the checks of the selected run tier (`unit`, `pr` or `full`; default `pr`), optionally filtered by `--only <glob>`, bring the stack up as needed, and exit 0 only if every executed check passed and every time budget of the run held. On a run that isn't full (filtered, tier `unit`, or tier `pr` whose selection isn't FULL), `report.full_run` records `skip` ("not a full run") instead of `fail`, and that skip doesn't affect the exit code. A run counts as **full** only if it is unfiltered and its tier is `full`, and it SHALL include every check and every unit test referenced by any scenario in the archived specs and in the active changes (those listed in `harness/active-changes`). A `pr` run whose selection is FULL SHALL execute every unit, integration and journey check (the complete acceptance suite) and only the gate checks its changed paths select.

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
Every check SHALL declare exactly one **layer**: `unit` (no server, no client, no Wine process; a check that starts one fails), `integration` (server probe, SQL, CLI or a stand-in `./play`), `journey` (the real client or the app end to end; only the named set in "Fixed journey set") or `gate` (builds from empty, soaks, packaging, real-stack harness self-tests). Run tiers SHALL be: `--tier unit` runs the unit layer only (`harness/hs test` is the same run); `--tier pr` runs the unit layer, then the integration, journey and gate checks the impact selection picks; `--tier full` runs every check of every layer. `results.json` SHALL record the tier, each record's layer, and the selection.

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
A `pr` run SHALL select tests from the paths the change touches (`git diff --name-only --no-renames <merge-base with origin/main>` plus staged, unstaged and untracked files). A path SHALL select: (1) FULL (the complete acceptance suite) if it matches the FULL-trigger list (`patches/core/**`, `patches/playerbots/**`, `fork.lock`, `bootstrap`, `harness/hslib/build.py`, `harness/hslib/stack.py`, `harness/hslib/probe/**`, and `harness/hslib/config.py` or `harness/hslib/paths.py` unless rule 4 applies); (2) nothing beyond the unit layer if it matches the no-impact list (`openspec/**`, `docs/**`, `*.md`, `harness/tests/unit/**`); (3) otherwise every integration, journey and gate check that declares the path in its `covers` globs or whose own file's import closure contains it, plus `journey.first_play` when any journey's import closure contains it; (4) a change to `config.py` or `paths.py` that only adds, removes or changes top-level assignments selects the checks whose import closure references those names, and any other change to them is rule 1. A path that rule 3 maps to no test SHALL select FULL. A git failure, an unparseable source, or a stale `covers` glob (matching no file) SHALL select FULL. The selection, the reason for each selected test and the selection's own duration SHALL be recorded.

#### Scenario: Unknown, docs, owned and shared paths
- **GIVEN** synthetic changed-path sets on a scratch git repo
- **WHEN** the selector runs for (a) `docs/x.md` only, (b) `modules/mod-moonglade/src/MoongladePause.cpp`, (c) a new file under `harness/hslib/` that nothing imports and no `covers` names, (d) `patches/core/0003-world-pause.patch`, (e) a git failure, (f) a shared helper imported only by `harness/hslib/checks/playclient_util.py`, (g) a new constant `SAVES_X` in `config.py` read only by `saves.py`, and (h) a changed function body in `config.py`
- **THEN** (a) selects only the unit layer; (b) selects the pause integration tests, `journey.pause` and `journey.first_play`, and no saves test; (c), (d), (e) and (h) select FULL with the reason recorded; (f) selects every journey and check importing that helper; (g) selects the checks whose closure contains `saves.py` and no pause test
- **AND** the selection step took at most 10 s, recorded
- Check: `report.selection`

### Requirement: Fixed journey set
The `journey` layer SHALL be exactly this named set, each with a time budget, run one at a time with no other check overlapping it: `journey.first_play` (150 s; cold app open → fresh play client → Continue → character select → world → quit from the Dock → shutdown window → backup → everything down), `journey.pause` (90 s), `journey.saves` (150 s), `journey.login_recovery` (120 s), `journey.stock_client` (60 s), `journey.app_desktop` (240 s; GUI, stand-in client). The set SHALL hold at most 8 journeys and at most 12 min of budgets in total; adding, removing or re-budgeting a journey is a spec change.

#### Scenario: The registry matches the named set and journeys never overlap
- **GIVEN** the registry and a completed full run
- **WHEN** the journey rule runs
- **THEN** the registered journey-layer checks are exactly the six named ids with the budgets above, their budgets sum to at most 720 s, and the run's timeline shows no two journeys and no journey and another check overlapping
- **AND** a scratch registry with a seventh unnamed journey fails the rule
- Check: `report.journeys`

### Requirement: Every layer is timed and budgeted
Every run SHALL record the wall time of the unit layer, the selection step, the integration layer, each journey, the gate checks and build time, and SHALL fail (`report.layer_budgets`) when: the unit layer took more than 120 s; the selection took more than 10 s; a journey exceeded its budget; a full run, or a pr run whose selection is FULL, spent more than 30 min in integration and journeys (build time and gate checks excluded); or the checks of this run that aren't in the timing baseline and that the selection picked add more than 120 s (the "a new feature adds at most 2 min" rule). The summary SHALL show each layer's time against its budget.

#### Scenario: Each budget miss fails the run
- **GIVEN** synthetic run records
- **WHEN** (a) the unit layer took 121 s, (b) selection 11 s, (c) `journey.pause` 95 s, (d) a FULL-selection pr run 31 min of integration and journeys, (e) a full run 31 min of them with 10 min of build time on top, (f) a pr run with 130 s of new selected checks, (g) a full run of 29 min plus 25 min of build and gate time
- **THEN** (a) through (f) each fail naming the layer, its time and its budget; (g) passes with the build and gate time reported
- Check: `report.layer_budgets`

### Requirement: The per-PR acceptance average stays at or under 15 minutes
Every `pr` run SHALL append its integration-plus-journey time, whether its selection was FULL, its changed paths and the repo SHA to the run history (`harness/out/pr-runs.jsonl`). From the fifth recorded run on, a `pr` run SHALL fail `report.pr_average` when the mean of the last 10 recorded pr runs (this one included) exceeds 15 min; with fewer than 5 recorded runs it SHALL report, without failing, the mean of the selector's replay of the last 15 merged PRs (each PR's changed paths, per-check times from the last full run). The summary SHALL show the mean, the share of FULL selections and the slowest runs. A run whose only failure is `report.pr_average` MAY merge only after the miss is recorded in STATUS.md as a process item with a named remedy (CLAUDE.md §9); it is never reported as a pass.

#### Scenario: The rolling average fails when it drifts
- **GIVEN** a scratch run history of 9 pr runs averaging 14 min
- **WHEN** a 25 min pr run completes, and separately a 15 min one
- **THEN** the first fails `report.pr_average` (mean 15.1 min), naming the mean, the FULL share and the slowest runs; the second passes (mean 14.1 min)
- **AND** with 3 recorded runs it reports the replay's mean and the PRs it replayed with status pass and the note "fewer than 5 recorded runs"
- Check: `report.pr_average`

### Requirement: Gate-layer checks
Gate checks SHALL run in every full run and in a `pr` run whose changed paths select them; a `pr` run SHALL never run an unselected gate check. A change whose spec deltas reference a gate check SHALL be verified with at least one run that executes it before it merges. Gate checks on the fixed-cost allowlist (build from empty, release packaging, population and performance soaks), each with a recorded reason, are excluded from the pr and full layer budgets and stay under the per-check timing guard.

#### Scenario: Gate checks run when selected or full, never otherwise
- **GIVEN** a gate check whose `covers` names `harness/hslib/lanes.py`
- **WHEN** a pr run with only `docs/x.md` changed, a pr run with `harness/hslib/lanes.py` changed, and a full run execute
- **THEN** the first doesn't run it and records nothing for it, the second and third run it and require it to pass
- Check: `report.gate_rule`

### Requirement: Acceptance tests don't sleep through fixed waits
Acceptance-layer code (integration and journey checks and the helpers they import) SHALL NOT wait with a fixed sleep longer than 1 s unless the wait is on a reviewed allowlist naming the spec that requires the real time (for example the 20 s pause, the 20 s logout countdown, the 30 s stay); other waits SHALL poll for the event with a timeout that only bounds them.

#### Scenario: A new long sleep fails the lint
- **GIVEN** the acceptance sources
- **WHEN** the lint scans them (calls to `time.sleep`, `wait_until` and the play-run waits with a literal or named constant over 1 s)
- **THEN** every such wait is on the allowlist with its spec citation
- **AND** a scratch copy with an added `time.sleep(5)` in a check fails, naming the file and line
- Check: `report.no_fixed_sleeps`
