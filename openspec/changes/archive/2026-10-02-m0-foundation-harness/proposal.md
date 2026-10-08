# Proposal: M0 Foundation — build, boot, and verification harness

## Why
Nothing can be verified until a headless harness can build the server, boot it, drive it, and assert on its state. The harness is the oracle for every later milestone (CLAUDE.md §5 via `docs/harness-requirements.md`), so it comes first.

## What Changes
- A pinned "fork" of the mod-playerbots AzerothCore core (ADR-0001), built natively on macOS ARM64 with mod-playerbots compiled in (ADR-0002).
- A repo-local, isolated MySQL instance and server runtime under `env/`, `data/`, `server/` (all gitignored).
- `harness/hs`: one entry point for build, lifecycle, GM commands (SOAP), SQL assertions, test fixtures, and `run` (all checks → `harness/results.json` + `harness/summary.md`).
- A headless 3.3.5a (build 12340) protocol probe that logs in, lists realms, and enumerates/creates characters. It's the automated stand-in for "client connects".
- A client config generator (`realmlist.wtf`/`Config.wtf`) for Zach's CrossOver client.
- Research report: can mod-playerbots be scripted to run content as a behavioral test (non-blocking).

## Capabilities

### New Capabilities
- `server-build`: building the pinned core + modules from clean on macOS ARM64.
- `server-lifecycle`: starting/stopping DB, authserver, worldserver with readiness detection, fail-fast, and clean shutdown.
- `harness-control`: GM command execution, SQL assertions, test fixtures (account, character, state changes).
- `harness-reporting`: the single-command run and its machine-readable/human reports.
- `client-connectivity`: localhost realm config, protocol-level login probe, client config generation, and the human login gate.

### Modified Capabilities
(none — greenfield)

## Impact
New dirs: `harness/`, `docs/`, `patches/`, `modules/`. Gitignored runtime dirs: `vendor/`, `build/`, `server/`, `env/`, `data/`. Uses Homebrew deps (boost, ccache). Ports bound to 127.0.0.1 only: 3307 (MySQL), 3724 (auth), 8085 (world), 7878 (SOAP).

## Spec-change log
Any later change to a scenario is recorded here with a reason, per CLAUDE.md §10.

- 2026-10-01 · `client.protocol_badpass`: "incorrect-password result" replaced with the server's actual observable behavior. AzerothCore never sends `WOW_FAIL_INCORRECT_PASSWORD` (0x05). For a wrong password it accepts the challenge and rejects the proof with `WOW_FAIL_UNKNOWN_ACCOUNT` (0x04) (`vendor/azerothcore-wotlk/src/server/apps/authserver/Server/AuthSession.cpp` HandleLogonProof, SRP6 verify-fail branch). This is a clarification, not a loosening: the check also asserts that no session key is issued and that world auth is refused. Raised by builder B1. To be reviewed at the M0 gate.
- 2026-10-01 · Added scenarios `client.inspect` and `client.launch_smoke` (client install/integrity and Wine launch) after Zach supplied a "clean 3.3.5a" client (no upstream checksums, so integrity is pinned at install time). This adds coverage and loosens nothing. The previous verifier PASS covers the other 25 scenarios; these two need their own verifier pass.
- 2026-10-01 · `client.launch_smoke`: "(it has reached the login screen without crashing)" overstated what a liveness check proves. Replaced with an observable requirement (an on-screen game window ≥640×480 owned by Wow.exe, plus no crash report), which the check now enforces, and a note that the login-screen content is confirmed at the human gate. The check is stronger, and the spec's claim is now accurate rather than loosened.
- 2026-10-01 · `client.launch_smoke`: added "no Wine graphics errors during the run" after F3. The D3D9 path logged 62k wined3d `GL_INVALID_OPERATION` errors and drew red/blue smears that the old check couldn't see. This tightens the check.
- 2026-10-01 · `client.inspect`: the source-unchanged clause now rests on the install-time before/after stats. A source that its owner deletes afterwards is recorded, not failed (Zach removed the 17.7 GB zip from Downloads). The clause's purpose, proving the harness never modifies user data outside the repo (§11), is fully kept. Only the impossible "re-stat a file the user deleted" requirement is gone.
- 2026-10-02 · `report.coverage` / `report.full_run`: "active changes" was undefined, so drafting the M1–M3 specs made every full run fail on checks nobody had started building yet. Defined as the changes listed in `harness/active-changes`. Planned changes and their unregistered checks are still reported in the evidence. The rule that a change can't be approved without full coverage is unchanged, because a change must be listed (and so covered) while it's built and verified.
- 2026-10-01 · Verifier cycle 3 fixes. `client.inspect` now fails on any `.exe`/`.dll` not present at install (tightening; it was computed but not enforced). `client.launch_smoke` also counts mtld3d's `ERROR mtld3d` lines as graphics errors (tightening; the default layer logs Rust-style). `report.coverage` fails if a change with ticked tasks isn't listed in `harness/active-changes` (tightening). The client-connectivity wording "CrossOver" became "Wine (ADR-0004)" to match the accepted Wine-layer decision; no behaviour changed.
