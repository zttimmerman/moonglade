# power-guard Specification

## Purpose
TBD - created by archiving change power-guard. Update Purpose after archive.

## Requirements

### Requirement: Build job cap
`hs build`, `hs bootstrap` and `./bootstrap` SHALL default the build's job count to the Mac's performance-core count (from `sysctl hw.perflevel0.physicalcpu`; if that is missing, half of `hw.ncpu`, at least 2), never more than the total core count. `--jobs N` SHALL override it. The build log SHALL record the job count and where it came from.

#### Scenario: Default job count follows performance cores
- **GIVEN** a stubbed core probe reporting 6 performance cores and 10 total
- **WHEN** a build plan is computed with no `--jobs`
- **THEN** every `cmake --build` step uses `-j 6` and the log line names "6 performance cores"
- Check: `power.jobs_default`

#### Scenario: Missing probe falls back safely
- **GIVEN** a stubbed core probe with no performance-core value and 10 total
- **WHEN** a build plan is computed
- **THEN** the job count is 5; with 2 total cores it is 2
- Check: `power.jobs_default`

#### Scenario: Override is honoured on every entry point
- **GIVEN** no stack is running
- **WHEN** `hs build --jobs 3` and `./bootstrap --jobs 3` compute their build plans (plan-only test switch)
- **THEN** both use `-j 3`, and a value below 1 is refused with a plain message
- Check: `power.jobs_override`

### Requirement: Battery pause
During heavy work (before each build step, and before each check in a full, slow or gui harness run) the harness SHALL read the battery. Below 35 %, whether on battery or plugged in, it SHALL pause, print one plain line ("Paused: battery at N %. Moonglade will continue at 50 %; plug in a stronger charger if this keeps happening."), and resume on its own when the battery reaches 50 %. Each pause SHALL be recorded (start, end, percent) in the build log or the run's results. `--ignore-power` SHALL skip pausing. Unknown battery readings (desktop Macs) SHALL never pause.

#### Scenario: Low battery while plugged in pauses then resumes
- **GIVEN** a stubbed battery sequence 40 % (plugged in), 34 %, 38 %, 49 %, 50 %
- **WHEN** a guarded multi-step job runs
- **THEN** it pauses before the step that saw 34 %, prints the pause line once, resumes when 50 % is read, completes every step, and records one pause with its start, end and percents
- Check: `power.pause_resume`

#### Scenario: Desktop Mac never pauses
- **GIVEN** a stubbed battery probe with no battery (unknown percent)
- **WHEN** a guarded job runs
- **THEN** it never pauses
- Check: `power.pause_resume`

#### Scenario: Override skips the pause
- **GIVEN** a stubbed battery at 20 %, plugged in
- **WHEN** a guarded job runs with `--ignore-power`
- **THEN** it runs without pausing and the log says the guard was skipped
- Check: `power.pause_resume`

#### Scenario: Harness run pauses between checks
- **GIVEN** a nested harness run of three file-only checks with a stubbed battery dropping to 30 % after the first
- **WHEN** it runs
- **THEN** it pauses before the second check, resumes at 50 %, all three pass, and `results.json` lists the pause
- Check: `power.run_pause`

### Requirement: Stop cleanly before the Mac shuts down
If the battery reaches 15 % or lower during a pause, the guarded work SHALL stop with a nonzero exit and a plain message naming what stopped, why, and how to resume. A stopped bootstrap SHALL resume from that step on its next run.

#### Scenario: Falling battery stops the job
- **GIVEN** a stubbed battery sequence 34 %, 20 %, 15 %
- **WHEN** a guarded job runs
- **THEN** it exits nonzero with the stop message, no later step runs, and the record shows the stop percent
- Check: `power.stop_low`

#### Scenario: Stopped bootstrap resumes
- **GIVEN** a bootstrap stopped by the power guard at its server-build step (plan-only test switch, stubbed battery)
- **WHEN** bootstrap runs again with a healthy stubbed battery
- **THEN** it starts at the server-build step, not at the beginning
- Check: `power.bootstrap_resume`

### Requirement: Test switches can't leak
The power guard's test switches (stubbed core probe, stubbed battery sequence, plan-only) SHALL be refused by `./play`, `./bootstrap`, `hs` and the app outside a nested harness run.

#### Scenario: Switches refused outside the harness
- **GIVEN** the power-guard test switches set in the environment
- **WHEN** `hs build`, `./bootstrap` and `./play` start outside a harness run
- **THEN** each refuses with a message naming the switch, before doing any work
- Check: `power.switch_refusal`
