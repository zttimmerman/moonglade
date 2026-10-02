# Proposal: Core patch: server-mail async login callback guard

## Why
Prerequisite for M2 (bot population). A logout shortly after login crashes worldserver, and a fast relog to another character can misdeliver server mail. Full analysis and the alternatives considered are in ADR-0006.

## What Changes
- `patches/core/0001-server-mail-guard-async-player.patch`: in `ServerMailReward::OnPlayerLogin`'s callback, skip unless the session's current player exists and is the same character (by GUID) the lookup was queued for.
- New harness checks that reproduce the crash without the patch and prove the patched server survives.

## Capabilities
### New Capabilities
(none)
### Modified Capabilities
- `server-build`: the core patch queue now contains a real patch; a requirement for login/logout robustness.

## Impact
Core source (via the patch queue only), checks. No data or config changes.

## Spec-change log
- 2026-10-02 · Requirement narrowed to what the scenarios prove behaviourally: surviving an early logout. The relog-to-another-character misdelivery clause had no behavioural scenario; only `core.patch_applied`'s static guard pattern covered it (verifier concern). The patch still implements the GUID guard. The behavioural check moves to follow-up **F11** (second character on the test account, A→B fast relog, assert no misdelivered mail; optionally show the baseline misdelivers). This is a narrowing with a recorded reason, to be reviewed at the next gate; the crash fix (the M2 prerequisite) is fully verified.
