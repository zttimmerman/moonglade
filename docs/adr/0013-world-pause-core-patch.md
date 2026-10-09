# ADR-0013: Core patch: a world-pause switch

- Status: proposed (2026-10-09). It becomes accepted with the `true-pause` verifier PASS. Patch: `patches/core/0003-world-pause.patch`. Checks: `pause.*` and `client.pause_menu`.
- Date: 2026-10-09

## Context
Zach wants a true single-player pause: Esc stops the world, and closing the menu resumes it exactly where it was (decisions recorded in `openspec/changes/true-pause/proposal.md`). CLAUDE.md §2 allows core patches only when unavoidable, each with an ADR.

The spike (`docs/research/pause-spike.md`, branch `spike/pause`) found:
- **No prior art.** The upstream search on 2026-10-09 covered AzerothCore, the playerbots core fork, mod-playerbots, TrinityCore, VMaNGOS and CMaNGOS. No world-pause feature or request exists, so there's no pin bump or cherry-pick to prefer. The order of preference is pin bump > cherry-pick > own patch.
- **No module hook can change or skip the elapsed time.** Every `*Update` script hook takes `diff` by value and runs beside the work (spike §2.5). A module-only "freeze bubble" leaves buffs, DoTs, cooldowns and respawns running, so it isn't a pause.
- **Two clocks.** Most timers advance by the per-tick `diff`. Creature cooldowns, respawns, leash, diminishing returns and similar timers read `GameTime`, which today is the wall clock. Player cooldowns are set from `GameTime` but checked against the raw clock (`Player.cpp:16708-16723`).

## Decision
Patch the core with one explicit switch, design A in the spike:
- `World::SetPaused/IsPaused` (atomic).
- While paused, the simulation updates get `diff = 0`. Sessions, the DB keep-alive, query callbacks and CLI/SOAP keep real time.
- `Map::Update` still processes player sessions but skips `Player::Update`, so playerbots make no stray decisions.
- `GameTime` stops and keeps a paused-total offset.
- The 3 player-cooldown checks read `GameTime::GetGameTimeMS()`. This also fixes a latent inconsistency, and it's a candidate for upstream (F17, only with Zach's OK).

All pause policy lives in `mod-moonglade`: the toggle, rooting the player, cancelling the cast, the taxi refusal, the resume resync and the config switch.

Rejected alternatives:
- **A generic `OnBeforeWorldUpdate(uint32& diff)` hook.** It's more upstream-friendly, but it still needs a core branch in `Map::Update`, and it's harder to verify.
- **Zero diff alone.** Cooldowns, respawns and leash keep running on the clock.
- **A freeze bubble or GM commands.** These are not a pause.
- **Stopping the server process (SIGSTOP).** It causes a time skip on resume and the client disconnects.

## Consequences
- A small patch (~60–100 lines in 4 files) to carry across core pin bumps. `core.patch_applied` covers it.
- Daily resets, mail and the auction house slip by the total paused time. That's acceptable for a single player.
- Client-animated timers visibly correct on resume. The `[human-verify]` scenario covers how that looks.
- Measured facts from the prototype (task 1.1) are below.

## Measured (prototype, task 1.1, 2026-10-09)
Details: `openspec/changes/true-pause/design.md` "Prototype (task 1.1)". A probe character in melee with a creature, a buff on it, a DoT on the creature, a player and a creature spell cooldown, paused ~27 s twice with a bare toggle (no module policy):
- **Frozen on the server:** buff and DoT remaining durations, the player cooldown (`Player::GetSpellCooldownDelay`, after the 3-line fix) and the creature cooldown (`Creature::GetSpellCooldown`, `GameTime`) read identical values 13 s apart while paused; creature health and position unchanged.
- **Nothing reached the client while paused** except GM command replies: no melee, no DoT ticks (`SMSG_PERIODICAURALOG`), no `SMSG_MONSTER_MOVE`.
- **Resume:** combat picked up at once (8 melee swings and 4 DoT ticks in the next 5 s); all values counted down again from where they stopped. The paused-total offset matched the wall clock (26948 ms for a 26.94 s pause).
- **The connection held** with keep-alives only.
- **A cast in progress froze** (no `SMSG_SPELL_GO` while paused). The module cancels casts on pause (Zach, 2026-10-09).
- **The client is told nothing on resume** by the core alone: no aura or cooldown packets. The module's resume resync is needed for the client's timers.
- Two patch changes came out of it: the shutdown countdown runs on the wall clock (otherwise `server shutdown N`, which `hs down` uses, never finishes while paused), and the switch sits in a new small header (`game/Time/WorldPause.h`) instead of `World.h`, so a rebuild recompiles 5 files.
- Not measured: proc internal cooldowns stay on the raw clock (as decided; they elapse during a pause, in the player's favour).
