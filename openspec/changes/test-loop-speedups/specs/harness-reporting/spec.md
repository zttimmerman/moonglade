## ADDED Requirements

### Requirement: The GUI preflight only guards runs that need the screen
An unfiltered full, slow or gui run SHALL refuse to start because of a locked screen or a sleeping display only when the run's GUI rule says GUI-marked checks will execute. Low battery while on battery power SHALL always refuse. `--ignore-gui-preflight` overrides both.

#### Scenario: Locked screen, no GUI checks due
- GIVEN a checkout whose GUI rule records the GUI tier as skipped by design, and a probe reporting the screen locked
- WHEN an unfiltered full run starts
- THEN the run is not refused, and the screen state is recorded in the run's evidence
- Check: `report.gui_preflight`

#### Scenario: Locked screen, GUI checks due
- GIVEN a checkout whose GUI rule says the GUI checks will run (or `--tier gui`), and a probe reporting the screen locked or the display asleep
- WHEN the run starts
- THEN it exits 2 within 5 seconds with a one-line reason, before any check runs
- AND on battery power below 35% it refuses whatever the GUI rule says
- Check: `report.gui_preflight`

### Requirement: Runs keep the display awake
Every `hs run` SHALL hold a display-sleep prevention assertion for its whole duration, released when the run exits, including on error. `hs keep-awake [--hours N]` SHALL hold the same assertion for N hours (default 8) or until stopped.

#### Scenario: The assertion is held during a run and released after
- GIVEN no Moonglade-owned display-sleep assertion
- WHEN `hs run` executes a check
- THEN during the check `pmset -g assertions` lists a PreventUserIdleDisplaySleep assertion owned by a process of this run
- AND within 5 seconds after the run exits, no assertion from this run remains, including after a run that errors
- Check: `report.keep_awake`

### Requirement: The last full run's results are kept
Every unfiltered full run SHALL write its results.json and summary.md to `harness/out/last-full-run/`. Filtered, quick or backups runs SHALL never overwrite them. `hs timing-baseline join` SHALL default to that copy.

#### Scenario: A later filtered run doesn't overwrite the full run
- GIVEN an unfiltered full run has completed
- WHEN a filtered run (`--only`) completes afterwards
- THEN `harness/out/last-full-run/results.json` still holds the full run (`full_run: true`, its repo sha and start time)
- AND `hs timing-baseline join --label x` without a results path reads that copy
- Check: `report.last_full_run`

### Requirement: A killed run's processes don't block the next run
When a harness run is killed, the next run SHALL identify the processes the dead run started (`./play`, Wine, Wow, the servers) and stop them cleanly before its checks start. Processes not started by a harness run, such as the player's own `./play` or the app, SHALL never be stopped.

#### Scenario: SIGKILL mid-run, then a new run
- GIVEN a harness run is SIGKILLed while its `./play` child and the stack are running
- WHEN the next `hs run` starts
- THEN it records the orphans it found and stops them, and its checks run normally
- AND a `./play` started outside the harness is left running and the run refuses as today
- Check: `report.orphans_healed`

### Requirement: Nightly flake hunt
`hs nightly` SHALL run the unfiltered full run 3 times on `main` and write `harness/out/nightly/<date>/`, with each run's results and a flake table listing every check whose status differed between runs. It SHALL skip, with a recorded reason and no runs, when the main checkout isn't a clean `main`, when the stack or another harness run is in use, or when on battery below 35%. `--install` and `--uninstall` manage a user LaunchAgent. Agents SHALL NOT install it.

#### Scenario: Three runs, flake table, and safe skipping
- GIVEN stubbed full runs where one check passes, fails, then passes
- WHEN `hs nightly` runs
- THEN the flake table lists exactly that check with its three statuses, and the summary names it
- AND with the checkout on a branch, or dirty, or with a harness run active, it records the skip reason and starts no run
- Check: `report.nightly`

### Requirement: Full run under 25 minutes
An unfiltered full run with the GUI and backups tiers skipped by design SHALL finish in under 25 minutes of wall time on Zach's Mac. Checks may run alongside each other only where they share no exclusive resource: the stack, the client, the screen, or a fixed port. Each check's assertions stay unchanged.

#### Scenario: Wall time and unchanged results
- GIVEN the full check list
- WHEN an unfiltered full run executes with nothing GUI- or backups-related changed
- THEN `wall_s` is under 1500, and every check's status equals its status in a serial run of the same commit
- Check: `report.full_run_wall`
