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

## Prototype (task 1.1)
Measured 2026-10-09 on a throwaway build: the core switch as drafted for `patches/core/0003` (World::Update diff gating, `Map::Update` sessions-only branch, `GameTime` freeze with a paused-total offset, the 3 `Player.cpp` checks), toggled by a bare `.moonglade pause on|off|status` command with no module policy (no root, no cast cancel, no resync), plus a GM read-out of a unit's remaining cooldown. Probe session on the companion harness account (HSCOMP, `Hscomp`, level 8 warrior, Elwynn Forest), in melee with a Mangy Wolf (auto-attack both ways). Spells picked by DBC rule (scratch scan of Spell.dbc / SpellDuration.dbc / SpellCastTimes.dbc): buff 3369 (one APPLY_AURA MOD_STAT effect on the caster, 120 s), DoT 744 (one APPLY_AURA PERIODIC_DAMAGE effect, 30 s, 3 s ticks), player cooldown 55198 (`.cast`, 180 s recovery), creature cooldown 17116 (`.cast back`, then `.unaura`: a cooldown-starts-on-event spell, 180 s), cast-time spell 12655 (2 s cast). Two runs, each paused ~27 s.

| What | Measured during the pause | After resume |
|---|---|---|
| Buff remaining (`.list auras id`) | identical in readouts 13 s apart (111779 = 111779 ms; run 2: 111646 = 111646) | counts down again (1 s later 110749) |
| DoT on the creature | duration identical (22604 = 22604 ms); **no** `SMSG_PERIODICAURALOG` | 4 ticks in the next 5 s |
| Player cooldown (`GetSpellCooldownDelay`) | identical (172189 = 172189 ms) | counts down |
| Creature cooldown (`Creature::GetSpellCooldown`) | identical (173466 = 173466 ms) | counts down |
| Creature health, position | unchanged (67 hp, same x/y/z) | melee resumes: 8 `SMSG_ATTACKERSTATEUPDATE` in 5 s |
| Packets to the client | only GM chat replies and 4 `SMSG_COMPRESSED_UPDATE_OBJECT` (from the GM commands); no melee, no periodic log, no `SMSG_MONSTER_MOVE` | 31–39 `SMSG_MONSTER_MOVE`, combat log, time sync in 5 s |
| A 2 s cast started 0.6 s before the pause | no `SMSG_SPELL_GO` during 5 s paused (the cast froze) | the cast failed after resume (`SMSG_CAST_FAILED` result 0x28) — moot, the module cancels casts on pause |
| Session | stayed in the world, keep-alives only | normal |
| `.moonglade pause status` paused total | 26416 ms at 26.4 s of pause | 26948 ms after a 26.94 s pause (matches the wall clock) |

**What the client is told on resume without the module:** nothing. No `SMSG_AURA_UPDATE` for the buff or the DoT (the only aura updates in the 5 s after resume were a periodic refresh of an unrelated 5 s aura), no `SMSG_SPELL_COOLDOWN` / `SMSG_MODIFY_COOLDOWN`. So the client's buff timers and cooldown spirals stay ahead of the server by the paused time until the module resyncs them (task 2.2).

Not measured in the prototype: proc internal cooldowns (they stay on the raw clock, as designed; they elapse during a pause, a small drift in the player's favour), respawn/leash and bots (covered by `pause.respawn_frozen` and `pause.bots_frozen`), the 5-minute connection (`pause.session_alive`), and the play client's view (see "As built").

Two findings changed the patch:
- `World::_UpdateGameTime` counted the shutdown timer from `GameTime`, so a paused world would never finish a `server shutdown N` (the harness's graceful `hs down` uses `server shutdown 1` over SOAP). The patch counts it on the wall clock.
- The switch lives in a new header `game/Time/WorldPause.h` (`WorldPause::SetPaused/IsPaused/IsTickPaused/GetPausedTotal`, defined in `GameTime.cpp`) instead of `World.h`, which ~110 files include directly; an incremental rebuild then recompiles 5 core files. Behaviour is the design's `World::SetPaused/IsPaused`.

Verdict: design A holds. Everything that should freeze froze, the session stayed connected, no crash.
