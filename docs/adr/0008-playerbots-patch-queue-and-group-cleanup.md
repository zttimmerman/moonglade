# ADR-0008: mod-playerbots patch queue; `.playerbots bot remove` takes the companion out of the party

- Status: accepted (2026-10-03, M2 verifier PASS). Patch: `patches/playerbots/0001-remove-bot-from-group-on-logout.patch`; checks `core.playerbots_patches_applied`, `build.pin_enforced`, `build.clean_smart` (queue and hash coverage), `comp.add_remove` (behaviour)
- Date: 2026-10-03

## Context
- `.playerbots bot remove <name>` is the command a player types to dismiss a companion. It calls `PlayerbotHolder::LogoutPlayerBot` (`vendor/mod-playerbots/src/Bot/PlayerbotMgr.cpp:350`, from the `remove`/`rm`/`logout` branch at `:709-718`). That function queues `BotLogoutGroupCleanupOperation` (`src/Script/WorldThr/PlayerbotOperations.h:418-452`) and logs the bot out right away.
- The operation never removes the bot from the group. It also does nothing at all once the bot is gone: `Execute` returns early when `FindPlayer` fails, and `IsValid` skips it. The core keeps logged-out members. Result: the companion stays in the party as an offline member. Measured: SMSG_GROUP_LIST still lists it 30 s+ after the command (`comp.add_remove`, 5 of 5 runs).
- mod-playerbots is pinned in fork.lock and synced to that SHA, with no way to carry local changes (ADR-0001 covers only `patches/core`).

## Decision
1. **Patch queue.** `patches/playerbots/*.patch` are applied in order to `vendor/mod-playerbots` by `hs build` (`build.sync_vendor`), the same way as `patches/core`.
   - `build.patched_pin_state` enforces playerbots_sha + exactly this queue, in sync and in `build.pin_enforced`.
   - The build-input manifest has a `patches_playerbots` digest, so a patch change forces a from-empty build. `build.clean_smart` has a sandbox case for it, and `core.playerbots_patches_applied` checks that an edited patch changes the hash.
2. **The patch (minimal):**
   - `LogoutPlayerBot(guid, bool leaveGroup = false)`. Only the `remove` command passes `true`; master logout, random-bot rotation and the other callers are unchanged.
   - With `leaveGroup`, the bot's group GUID is captured while the bot is still online and passed to the cleanup operation.
   - The operation then removes the bot by GUID from that group (`sGroupMgr->GetGroupByGUID`, `IsMember`, `RemoveMember`). It runs in the world-thread operation processor, and `IsValid` keeps it alive after the bot logged out.
   - `Group::RemoveMember` disbands a party left with fewer than 2 members, so a 2-person party (master + one companion) is disbanded cleanly; the group pointer isn't used afterwards.

## Consequences
- Dismissing a companion now takes it out of the party, which is what a player expects. Removing the last companion disbands the party.
- Master logout is untouched: bots log out with the master and keep their party slot, as before, governed by `KeepAltsInGroup`.
- A playerbots pin bump must rebase the queue; `core.playerbots_patches_applied` fails if the patch stops applying.
- Consider upstreaming to mod-playerbots. No upstream issue or fix found as of 2026-10-03; candidate to report/upstream.
