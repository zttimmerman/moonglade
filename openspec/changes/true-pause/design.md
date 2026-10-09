## Design
Follow design A in `docs/research/pause-spike.md` (on `spike/pause`, sections 2.1–2.6, 3.1–3.3 and Q4).
- **Core** (`patches/core/0003-world-pause.patch`):
  - `World::SetPaused/IsPaused` is an atomic flag.
  - In `World::Update`, the simulation calls get `diff = 0` while paused: `MapMgr`, battlegrounds, LFG, battlefields, outdoor PvP, world state, `OnPlayerbotUpdate`, `OnWorldUpdate`. Sessions, the DB keep-alive, query callbacks and CLI/SOAP keep real time.
  - `Map::Update`'s paused branch processes map sessions and skips `player->Update`.
  - `GameTime` freezes and keeps a paused-total offset.
  - The 3 checks in `Player.cpp` (`:16708-16723`) read `GameTime::GetGameTimeMS()`.
  - Proc internal cooldowns stay on the raw clock unless the prototype shows they matter; record the result either way.
- **Module:** `mod-moonglade/src/MoongladePause.cpp`.
  - The toggle comes from the `MGPAUSE` addon whisper (handled in `OnPlayerCanUseChat`, which is thread-unsafe chat and so still runs while maps are paused), plus the `.moonglade pause` command.
  - Root the player and cancel their cast; refuse on a taxi or transport.
  - On resume, resync auras and cooldowns (`SMSG_MODIFY_COOLDOWN` or a re-send).
  - Unpause on logout.
- **Addon:** `client-mods/addons/Moonglade`: `GameMenuFrame` OnShow/OnHide, the banner, and unpausing before Logout or Exit.
- **Checks:** headless probe plus SOAP, except `client.pause_menu`, which is a play-client check. These go in the full tier. `pause.session_alive` (5 min) goes in the slow tier. The quick tier has about 4 s of headroom, so add nothing there.
