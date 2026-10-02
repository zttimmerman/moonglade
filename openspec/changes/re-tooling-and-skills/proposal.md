# Proposal: Read-only client analysis tooling (Ghidra) + project skills

## Why
Zach opened read-only Ghidra analysis (ADR-0005), and asked for it to become standing tooling, together with the workflows refined so far, so later sessions and agents reuse them. The first consumer is the login-screen spike (a fast follow after M1): does 3.3.5a verify its GlueXML files?

## What Changes
- `hs ghidra-install`: fetch the pinned Ghidra (fork.lock: version, URL, SHA-256), verify it, and unpack it into `env/`. Requires JDK 21 (Homebrew `openjdk@21`).
- `hs re import|decompile|strings|xrefs`: headless analysis of a **copy** of a client binary under `env/re/`. It only reports and never writes to `client/`.
- Project skills under `.claude/skills/`:
  - `hs-change-loop`: OpenSpec change → builder → verifier → PR, with branch-per-change.
  - `hs-harness-checks`: writing checks that can fail; counterexamples; evidence.
  - `hs-client-debugging`: client crash reports, Wine logs, HS_WINEDEBUG, window screenshots, Wine layers.
  - `hs-reverse-engineering`: the read-only Ghidra workflow and citation rules.

## Capabilities
### New Capabilities
- `client-analysis`: read-only reverse-engineering tooling for the client.

### Modified Capabilities
(none)

## Impact
New `harness/hslib/ghidra.py`, CLI subcommands, and one check. About 1 GB of tools under `env/`. Skills are docs only.

## Spec-change log
(none yet)
