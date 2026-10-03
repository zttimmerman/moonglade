# ADR-0006: Core patch: guard ServerMailReward's async login callback

- Status: accepted (2026-10-02). Patch: `patches/core/0001-server-mail-guard-async-player.patch` (first core patch); checks `core.patch_applied`, `core.login_logout_race`, `core.login_logout_race_baseline`
- Date: 2026-10-02

## Context
CLAUDE.md §2 allows core patches "only when unavoidable, and each core patch gets an ADR". B9 found a worldserver segfault while adding instant logout for the test account (harness-tiers, `design.md` §4). The cause is in `vendor/azerothcore-wotlk/src/server/scripts/World/server_mail.cpp` (core `7f12e89`):
- `ServerMailReward::OnPlayerLogin` queues one async SELECT per server-mail record on the session's query processor. Each callback captures the raw `WorldSession*` and calls `sServerMailMgr->SendServerMail(session->GetPlayer(), …)`. `SendServerMail` immediately evaluates conditions on that player.
- If the character has logged out before the callbacks run, `GetPlayer()` is null → null dereference → crash. The 9 shipped server mails with unmet conditions reopen this window on every login.
- Second defect, same spot: the session outlives a logout to character select. A fast relog into a **different** character would run the stale callbacks against the new character, using lookups made for the old one, which can misdeliver mail.
- The callbacks are owned by the session's QueryProcessor, so they can't outlive the session (no use-after-free).

## Why unavoidable (module/SQL alternatives considered)
- A module can't intercept or cancel another script's queued DB callbacks.
- Disabling the script means losing server mail entirely, which is a gameplay change.
- Emptying the server-mail table hides the trigger but leaves the bug for any future mail.
- Bots (M2) log in and out constantly, so the population work would hit this crash.

## Decision
Ship `patches/core/0001-server-mail-guard-async-player.patch`, applied by `hs build`'s existing patch queue: capture the character's GUID and, in the callback, return unless `session->GetPlayer()` is non-null **and** has that GUID. That's a few lines, no behaviour change otherwise. Consider upstreaming it to AzerothCore.

## Consequences
- The first entry in `patches/core/`. `build.pin_enforced` and the build-input hash already cover the patch queue (a patch change forces a from-empty build).
- The harness's `settle_login` workaround stays as defense in depth, unless the change decides otherwise with evidence.

## Upstream status (checked 2026-10-03)
- **Fixed upstream** in AzerothCore PR #27827 ("fix(Scripts/World): guard server mail login callback against logged-o…"), merged 2026-09-27, commit `3d28fa72d742`. It uses the same approach as our patch (capture the player GUID; skip unless `session->GetPlayer()` exists and matches).
- Our pinned core (`mod-playerbots/azerothcore-wotlk@7f12e89`, 2026-09-20) predates it, and the fork's last upstream sync (PR #257, 2026-09-24) also predates it. As of 2026-10-03 the fork's Playerbot branch is still 70 upstream commits behind that fix.
- **Retirement:** when `core_sha` is bumped to a fork commit that contains `3d28fa7`, drop `patches/core/0001-…`. Its `git apply` will fail and the build stops loudly, which is the intended signal.
