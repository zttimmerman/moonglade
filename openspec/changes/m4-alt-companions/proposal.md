## Why
Zach's "companion roster" idea (STATUS, new ideas #2, 2026-10-04) is a few named companions who persist, level with you and keep their gear across sessions, like RPG party members. "Party RPG" (2026-10-07) folds the cheapest version into M4: your own other characters, brought along as AI companions. mod-playerbots already supports this. "Account bots" are enabled (`AiPlayerbot.AllowAccountBots = 1`, `vendor/mod-playerbots/conf/playerbots.conf.dist:153-155`), and `.playerbots bot add <AltName>` logs an alt in as a companion with its own gear, talents and XP. What's missing is checks, smooth defaults, and protection for the alt's real gear and talents.

This is the third of three M4 changes (the split is explained in `m4-dungeon-scaling`'s proposal). It's independent of the other two: it needs no dungeon, and it can be built while they verify.

## What Zach gets (plain language)
- **Bring your own characters.** Type `/mg bring <name>` and one of your other characters joins your party as an AI companion. They follow, fight and heal like the generated companions, using their own gear, talents and spells. `/mg dismiss <name>` sends them home.
- **They keep what they earn.** XP, levels, loot and gold they pick up on the trip stay with them, just as if you had played them. Loading a save of your main character doesn't rewind them (your 2026-10-09 decision).
- **Their real gear and talents are safe.** By default the companion system won't hand an alt generated gear or change its talents unless you ask that character directly.
- **Only your own characters.** Characters from another account can't be pulled in. That also keeps the test harness's characters and yours apart.

## What Changes
- **Moonglade addon:** `/mg bring <name>` and `/mg dismiss <name>`. They send the stock `.playerbots bot add|remove <name>` (`PlayerbotMgr.cpp` `ProcessBotCommand` :675-720). Before that, they whisper `reset botAI` (F16: strategies persist per bot in `playerbots_db_store`).
- **Config:** `AiPlayerbot.AutoGearCommandAltBots = 0` (stock default 1, conf:591-593), following open question 1's default, so `autogear` (which replaces equipped items) can't hit a real character by accident.
- **Checks `alt.*`** and one GUI check. Harness characters only: two characters on the harness companion account. Never PLAYER's characters.

## Open questions for Zach (plain gameplay terms; the default ships if you don't answer)
1. **May the game give your alts free generated gear when they join as companions?** *Default:* no. Alts keep exactly the gear you gave them, and the free-gear command is turned off for them. (Generated companions still get gear automatically.) The alternative is to allow it, which helps an under-geared alt keep up but replaces items it's wearing.
2. **Should `/mg party` bring your own alts first?** When an alt fits a missing role and is within 5 levels of you, it would take that slot before a generated companion does. *Default:* no for now; you bring alts by name. This becomes a party-screen option after M4.
3. **If an alt is far below your level, should it be raised to your level for the trip?** *Default:* no. It keeps its own level (it may struggle), because raising it would permanently change your character. Generated companions are the ones that match your level.

**Zach's answers (2026-10-10):**
- Q1: no free gear for alts ("probably not").
- Q2: no; alts are brought by name. `/mg party` is Moonglade's own add-on command, which sends stock playerbots commands.
- Q3: no level raise.

## Sources
- mod-playerbots source and config at pin `7bae1b5c` (cited in design.md).
- Zach's decisions: STATUS "Party RPG" (2026-10-07); the saves decision that an alt keeps its trip's progress (2026-10-09).
- No game data values are written.

## Risks
- **Two characters from one account online at once.** The core allows that for playerbots logins. It works in today's `comp.*` setup for addclass accounts, but not yet for the master's own account. Task 1.1 confirms it.
- **Saves interplay.** An alt's progress must survive the main character's save load, and its own save list must stay consistent. This is covered by `alt.progress_kept` and the front-menu-saves spec ("loading … SHALL NOT change any other character").

## Spec-change log
- **2026-10-12 (loosening, for the M4 human gate).** Fewer game tests, per Zach's 2026-10-11 pipeline rule. The real-client GUI check `client.bring_slash` becomes a Lua unit test of the slash handler; the game side of the same command lists stays covered by `alt.bring_dismiss`. The `altgear-on` counterexample profile (a second server restart) becomes a unit test that every shipped profile sets `AutoGearCommandAltBots = 0`. What's lost: proof that the party frame draws the alt, and a live proof that the setting is what blocks autogear (the `autogear` refusal in `alt.gear_untouched` still shows it live).
