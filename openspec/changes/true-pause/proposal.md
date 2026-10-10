## Why
Zach wants a single-player feel: pressing Esc should stop the world the way a single-player RPG does, and closing the menu should pick up on the exact moment it left. Nobody has built this for AzerothCore, TrinityCore, CMaNGOS or VMaNGOS; the spike (`docs/research/pause-spike.md` on `spike/pause`, 2026-10-09) found no prior art. No module hook can stop the world's clock, so this needs a small core patch.

## Zach's decisions (2026-10-09)
- Pausing mid-cast **cancels** the cast.
- **Esc only.** The game menu opening pauses and closing it resumes. There is no separate pause key.
- **No auto-pause** when the game window loses focus. That can come later as a setting.

## What Changes
- **Core patch** `patches/core/0003-world-pause.patch` (ADR-0013). It adds a global "paused" switch:
  - while paused, the world simulation (maps, battlegrounds, LFG, playerbots hooks) gets zero elapsed time;
  - `Map::Update` still processes each player's packets but skips `Player::Update`;
  - the game clock stops and keeps a running "time spent paused" offset;
  - the 3 player-cooldown checks read the game clock instead of the raw clock. This fix is a candidate to send upstream.
- **mod-moonglade** `MoongladePause`:
  - toggles on the Moonglade addon's message, and adds a `.moonglade pause on|off|status` command;
  - roots the player and cancels their cast while paused;
  - refuses to pause on a taxi or transport;
  - unpauses before logout;
  - on resume, re-sends aura durations and cooldowns so the client's display corrects itself;
  - config key `Moonglade.Pause.Enable`, default 1.
- **Moonglade addon:** hooks `GameMenuFrame` show/hide, shows a "Paused" banner, and unpauses before Logout or Exit.

## Sources
- Spike: `docs/research/pause-spike.md`, which has the code paths with file:line, the upstream search (Q1, done 2026-10-09; no prior art) and the designs compared (design A is chosen).
- No game data values change.

## Risks
- Client-animated state (buff timers, cooldown spirals, a mid-step monster) visibly corrects on resume. The prototype (task 1.1) measures this first.
- Daily resets, mail and the auction house slip by the total paused time. That's acceptable.

## Spec-change log
- 2026-10-09 (tightening, Zach): "Esc pauses in the real client" adds a dimmed world view behind the menu, and `client.pause_menu` asserts the dimming and its removal on resume. Reason: the client keeps drawing idle animations and effects while the server is paused; Zach asked for the pause to read clearly as paused. Truly freezing client animations is a separate backlog research item (would need a client patch, its own ADR and Zach's yes).
