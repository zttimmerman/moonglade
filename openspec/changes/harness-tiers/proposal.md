# Proposal: Harness test tiers + faster full runs

## Why
A full run takes about 7 minutes: 3.5–5 min is the clean rebuild, about 55 s is the client smoke, and about 40 s goes to logout timers. Each verifier cycle runs it at least twice. M2 (bot warm-up and performance sampling) and M3 (mode restarts) will add 20+ minutes. Builders need a fast inner loop, and full runs should stay as fast as their guarantees allow. Zach approved this on 2026-10-02.

## What Changes
- **Tiers:**
  - `hs run --tier quick` (builders' inner loop): everything except checks tagged `full` or `slow`.
  - `hs run` / `--tier full` (the default and the merge gate): all checks except `slow`.
  - `--tier slow`: everything, including long behavioural or soak checks (nightly or on demand).
  - Each check declares its tier. `report.full_run` passes only for an unfiltered `full` or `slow` run.
- **Smarter clean build:** `build.clean` does a true from-empty build when the build inputs have changed since the last passing clean build (fork.lock, patches/core, modules/, cmake arguments, compiler identity), or with `--force-clean`. Otherwise it proves the installed binaries were produced from the current inputs (input-hash stamp) and runs an incremental build that must be a no-op.
- **Instant logout for the test account:** the harness test account gets only the instant-logout permission (no GM powers), which removes the 20 s logout timer from fixtures and the probe. The player's own account is never changed.

## Capabilities
### New Capabilities
(none)

### Modified Capabilities
- `harness-reporting`: run tiers; what a "full run" is.
- `server-build`: when a full run does a from-empty build.
- `harness-control`: the test account's logout permission.

## Impact
`harness/hslib/registry.py`, `runner.py`, `cli.py`, `checks/build_checks.py`, `fixtures.py` (or stack bootstrap), and the verifier brief (one verifier run must use `--force-clean`).

## Spec-change log
- 2026-10-02 · `build.clean`: a full run no longer always wipes `build/`. **This is a deliberate relaxation, recorded for Zach's review at the next gate.** Reason: a from-empty build only proves something new when its inputs changed. The input-hash stamp plus a no-op incremental build proves the binaries match the pinned inputs. Merges keep the full guarantee, because the verifier's brief requires at least one `--force-clean` run.
- 2026-10-02 · `report.full_run` on non-full runs: `skip` instead of `fail`, excluded from the exit code. Before this, every quick or `--only` run exited 1 by design, which made the builders' inner loop and scripted repeats awkward (raised by B9 and several verifiers). The merge guarantee is unchanged: a full run still requires `report.full_run` to pass.
- 2026-10-02 · The build-input hash covers only the server-build pins of fork.lock (core, playerbots), not unrelated pins (Ghidra, Wine runtime, data URLs), so bumping those doesn't force a from-empty build. This narrows the scope precisely; no build-relevant input is dropped.
