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

## As built
**Core** (`patches/core/0003-world-pause.patch`, 5 files, applies after 0001/0002 to core_sha 7f12e89):
- `game/Time/WorldPause.h` (new): `WorldPause::SetPaused/IsPaused` (the request, atomic, any thread), `IsTickPaused` (sampled once per tick by `BeginTick`, which `World::Update` calls first) and `GetPausedTotal`. Defined in `GameTime.cpp`. Kept out of `World.h`/`GameTime.h` so a rebuild recompiles 5 files (prototype finding).
- `World::Update`: `simDiff = paused ? 0 : diff` for `OnPlayerbotUpdate`, LFG (both calls), `MapMgr`, battlegrounds, outdoor PvP, world state, battlefields and `OnWorldUpdate`. Sessions, the world timers (DB ping, uptime, game events, who list), auctions, query callbacks and CLI/SOAP keep the real `diff`.
- `World::_UpdateGameTime`: the shutdown countdown counts wall-clock seconds, so `server shutdown N` (and `hs down`) still works while paused.
- `GameTime::UpdateGameTimers`: while the tick is paused nothing moves; on resume the paused span is added to a total that is subtracted from all four clocks (epoch seconds, ms since start, system and steady time points).
- `Map::Update`: a paused tick only runs each player's map-thread packets (`MapSessionFilter`) and `SendObjectUpdates`; no `Player::Update` (so playerbots decide nothing), creatures, respawns, scripts or weather.
- `Player.cpp`: `HasSpellCooldown`, `HasSpellItemCooldown`, `GetSpellCooldownDelay` read `GameTime::GetGameTimeMS()`.
- Proc internal cooldowns stay on the raw clock (not changed; they elapse during a pause).

**Module** (`mod-moonglade/src/MoongladePause.cpp`, config `Moonglade.Pause.Enable`, default 1, re-read on `reload config`; turning it off resumes):
- `OnPlayerCanUseChat(..., receiver)` swallows the add-on whisper `MGPAUSE` `1`/`0` to oneself. Answers by add-on whisper: `paused` (sent from `WorldScript::OnUpdate` in the first tick that runs paused, so nothing simulated follows it), `running`, `refused`, `disabled`.
- Pause: refused with a system line "You can't pause while travelling." on a taxi (`IsInFlight`) or a transport; otherwise `InterruptNonMeleeSpells(false)` (cast and channel cancelled, the client gets SPELL_FAILED_INTERRUPTED) and `SetControlled(true, UNIT_STATE_ROOT)` unless already rooted.
- Resume: unroot (only our root; `SetControlled` keeps an aura's root), then resync: `AuraApplication::ClientUpdate` for the player's, target's, pet's and same-map party members' visible auras, and for every client-visible player cooldown `SMSG_MODIFY_COOLDOWN` (+paused ms) when the client still runs it, else `SMSG_SPELL_COOLDOWN` with the server's remaining time.
- Logout: `ServerScript::CanPacketReceive` resumes on `CMSG_LOGOUT_REQUEST` before its handler starts the countdown; `OnPlayerLogout` of the pausing player resumes too.
- GM `.moonglade pause on|off|status` (console/SOAP too; from the console `on` pauses without a character to root or resync), plus harness read-outs `.moonglade pause unit` and `.moonglade pause cooldown <spell>` on the selected unit.

**Add-on** (`client-mods/addons/Moonglade/Moonglade.lua`, shipped by client patch 0003 as before): `GameMenuFrame` OnShow/OnHide send `1`/`0`; `PreClick` on Exit to Menu and Exit Game sends `0` first. On `paused` (and the menu still open) it shows the "Paused" banner (`FULLSCREEN_DIALOG`, top centre) and a black full-screen layer at 50 % (`HIGH`, under the menu), hidden on any other answer, when the menu closes and on leaving the world. Test mode `MoongladeTest.hook = "pause"` (planted only into a scratch play client) takes screenshots before Esc, while paused and after the resume.

**Checks** (`harness/hslib/pause.py` driver, `checks/pause_checks.py`): all headless ones use the companion account and character (GM level) except `pause.logout_unpauses`, which uses a new plain harness account `HSPAUSE`/`Hspause` (security 0, so the logout has the 20 s countdown). Spells, the killed creature and the flight path come from rules in `pause.py` (Spell.dbc etc., world DB), not memory. "During the pause" is judged by stream order (after the server's `paused` answer, before the resume request), because packets read together share a timestamp. Lost time uses the pause as the probe measured it, not the server's own counter. `pause.disabled_noop` restarts the worldserver with a new config profile `pause-off` (m0 + `Moonglade.Pause.Enable = 0`) and restores the previous profile. `client.pause_menu` drives `./play --account test` on a scratch play client and presses Esc through the existing in-prefix `SendInput` program (`clientinput.press_key`, `VK_ESCAPE`).

Deviation from the original outline: `pause.respawn_frozen` uses two pauses, one per GIVEN. The creature with the shortest respawn by rule (a Gnome Citizen in Ironforge, R = 35 s: 5 s respawn + 30 s corpse) has no creature near it that could chase the character, and creatures only update with a player near them (`Map::Update` marks the cells around players), so the character stands by each in turn; each pause is longer than R. Measured: respawned 1.6 s after R s of unpaused time; the chaser was still in combat 50 yd from home 6 s after a 40 s pause.

Measured timings (builder runs): combat 38–45 s, aura 46 s, cooldown 40 s, cast 29 s, respawn 146 s, bots 29 s, taxi 12 s, logout 23 s, disabled 57 s (two restarts), client.pause_menu 76–82 s, session_alive ~5.5 min (slow tier).

Play-client look (client.pause_menu screenshots, `harness/out/pause-menu-*.jpg`): with the menu open the world is dimmed, the game menu is in the centre and a dark box with gold "Paused" sits above it; after the second Esc the menu, the box and the dimming are gone and the view matches the pre-Esc shot (region brightness 46.2 before, 22.8 paused, 46.2 after). These runs had no fight, so the resume corrections of buff timers and cooldown spirals weren't seen on screen; that stays the `[human-verify]` scenario.

Known gaps (not in the spec): while paused, the player can still send instant spells and attacks (map-thread packets are processed), and with the game menu open the client may still accept action-bar keys; mirror timers (breath) aren't resynced; a `PLAYER` account named `HSPAUSE` would collide with the new harness account (not in `play.py`'s reserved list, to keep the play loop untouched).
