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
- 2026-10-01 · Added scenarios `client.inspect` and `client.launch_smoke` (client install/integrity and Wine launch) after Zach supplied the [redacted] "clean 3.3.5a" client (no upstream checksums, so integrity is pinned at install time). This adds coverage and loosens nothing. The previous verifier PASS covers the other 25 scenarios; these two need their own verifier pass.

