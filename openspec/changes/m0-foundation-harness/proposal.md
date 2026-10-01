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
(none yet — any later loosening of a scenario is recorded here with a reason, per CLAUDE.md §10)
