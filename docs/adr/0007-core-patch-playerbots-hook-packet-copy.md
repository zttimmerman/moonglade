# ADR-0007: Core patch: give the playerbots packet hook an intact copy of the packet

- Status: accepted (2026-10-03, M2 verifier PASS). Patch: `patches/core/0002-playerbots-hook-packet-copy.patch`; checks `core.playerbots_patches_applied` (patch content, stamp, hash) and `comp.quest_shared` (behaviour)
- Date: 2026-10-03

## Context
CLAUDE.md §2 allows core patches "only when unavoidable, and each core patch gets an ADR". B13 found that companions never accept a quest the player shares (m2-living-world, `comp.quest_shared`). The cause is in the playerbots fork of the core (`vendor/azerothcore-wotlk`, core `7f12e89`):
- mod-playerbots learns what the master does through `ServerScript::OnPacketReceived`. The core calls it *after* the opcode handler: `src/server/game/Server/WorldSession.cpp:467-474` (also 488-495, 501-508, 520-527), inside `#ifdef MOD_PLAYERBOTS`.
- Opcodes with a typed handler go through `PacketHandler<PacketClass>::Call` (`src/server/game/Server/Protocol/Opcodes.cpp:31-36`). That function move-constructs the typed packet from the `WorldPacket`, and the `ByteBuffer` move constructor empties its source (`src/server/shared/Packets/ByteBuffer.h:85-90`).
- So the hook gets an empty packet for every typed-handler opcode. `PacketHandlingHelper::AddPacket` drops empty packets (`vendor/mod-playerbots/src/Bot/PlayerbotAI.cpp:103-111`), and the bot's trigger never fires. Measured: the server answers SHARING_QUEST and sends the quest details to the bot, Playerbots.log never shows `T:quest share`, and the bot's quest log stays "Not Taken".

### Audit: opcodes the hook feeds to bots
The opcodes registered in `masterIncomingPacketHandlers` (`PlayerbotAI.cpp:167-182, 220-223`) plus the `PlayerbotMgr::HandleMasterIncomingPacket` switch (`PlayerbotMgr.cpp:1552-1585`), checked against their handlers in `Opcodes.cpp` / `WorldSession.h`:

| Opcode | Bot trigger | Handler (Opcodes.cpp line) | Broken before? | Fixed by 0002 |
|---|---|---|---|---|
| CMSG_PUSHQUESTTOPARTY | quest share | typed `PushQuestToParty` (544) | **yes**: companions never accept shared quests | yes |
| CMSG_QUEST_CONFIRM_ACCEPT | confirm quest | typed `QuestConfirmAcceptClient` (542) | **yes**: companions never confirm escort/shared-start quests | yes |
| CMSG_LOGOUT_REQUEST, CMSG_LOGOUT_CANCEL | PlayerbotMgr switch (log bots out / cancel) | typed `LogoutRequest` / `LogoutCancel` (206, 209) | no: the switch reads only `GetOpcode()`, which the move keeps | gets an intact copy too |
| CMSG_GAMEOBJ_USE, CMSG_AREATRIGGER, CMSG_GOSSIP_HELLO, CMSG_QUESTGIVER_HELLO, CMSG_ACTIVATETAXI, CMSG_ACTIVATETAXIEXPRESS, CMSG_GROUP_UNINVITE, CMSG_GROUP_UNINVITE_GUID, CMSG_LFG_TELEPORT, CMSG_CAST_SPELL, CMSG_REPOP_REQUEST, CMSG_RECLAIM_CORPSE, CMSG_QUESTGIVER_COMPLETE_QUEST, CMSG_QUESTGIVER_ACCEPT_QUEST | various | `WorldPacket&` handlers (308, 311, 510, 519, 560, 917, 248, 249, 1011, 433, 477, 597, 525, 524) | no: packet intact (the handlers do `rpos(0)` before reading) | unchanged behaviour; the copy starts at rpos 0 |
| CMSG_TAXICLEARALLNODES, CMSG_TAXICLEARNODE | taxi done | `STATUS_NEVER` (553, 708) | never delivered (unchanged) | n/a |

## Why unavoidable (module/SQL alternatives considered)
- The packet is consumed inside the core's dispatch before any script hook runs. `CanPacketReceive` runs earlier, but it gets a `const&`, and making a copy there would mean keeping per-session state in the module and matching it to the later hook. That's fragile, and it's also a module change.
- Converting the two typed handlers back to `WorldPacket&` is a larger core diff, and it would come back with every upstream conversion.
- Without it, a core companion feature (sharing quests with your party) silently doesn't work.

## Decision
Ship `patches/core/0002-playerbots-hook-packet-copy.patch`. At each of the four dispatch sites, under `#ifdef MOD_PLAYERBOTS` only, copy `*packet` into `hookPacket` before `opHandle->Call` and pass the copy to `OnPacketReceived`. The `STATUS_AUTHED` case gets a brace scope so the declaration doesn't cross a case label. That's 16 added lines and no change without playerbots. Consider upstreaming to mod-playerbots/azerothcore-wotlk. No upstream issue or fix found as of 2026-10-03 (mod-playerbots #142, 2024, only fixed the hook's compile signature); candidate to report/upstream.

## Consequences
- One packet copy per client packet while playerbots is compiled in. That's a byte-vector copy of a few bytes to a few KB, and only real-player sessions send client packets.
- The hook's copy starts at rpos 0. For `WorldPacket&` opcodes it used to arrive after the handler had advanced rpos; bot actions already reset rpos (e.g. `AcceptQuestShareAction`, `ConfirmQuestAction`), so behaviour is unchanged.
- The patch is covered by `core.playerbots_patches_applied`: it applies to core_sha after 0001, every hook site is patched, the installed build is stamped with it, and editing it changes the build-input hash. `build.pin_enforced` checks the whole core queue.
