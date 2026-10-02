# Proposal: M1 Offline loop — `./play`

## Why
KICKOFF §8 M1: a single `./play` command starts the DB, auth, and world, waits for readiness, launches the client pointed at localhost, and on client exit shuts everything down cleanly with no orphaned processes. It has to work with no network connection. M0 built every piece (`hs up`, `hs launch`, `hs down`); M1 composes them into the player-facing loop and proves the offline and cleanup properties.

## What Changes
- `./play` at the repo root: a thin wrapper over the harness that runs up → client-config → fixtures → launch Wow.exe under Wine (ADR-0004) → wait for the client to exit → graceful down.
- Signal handling: Ctrl-C or SIGTERM to `./play` also closes the client and shuts down cleanly.
- Single-instance lock: a second `./play` while one is running refuses and points at the running one.
- An offline guarantee: nothing in the loop needs the network. This is proven by running the loop with outbound network denied to its processes.
- Harness checks that drive `./play` with a scripted client stand-in, so the loop is testable headlessly.

## Capabilities
### New Capabilities
- `play-launcher`: the one-command offline play loop and its shutdown and cleanup guarantees.

### Modified Capabilities
(none)

## Impact
New `./play`, `harness/hslib/play.py`, and new checks. Reuses `stack`, `launch`, `fixtures`, `clientcfg`. No server or core changes.

## Spec-change log
(none yet)
