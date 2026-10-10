## Why
Zach wants saves that feel like a single-player RPG ("function similar to … Elder Scrolls", 2026-10-09): save whenever you like, quicksave and quickload, autosaves at safe moments, and load a save without closing the game. Today Moonglade only has whole-world backups (save-backups, PR #19): one is taken on every quit, and restoring one rewinds every character and needs the game closed. The `saves-channel` change (PR #31) proved that the title menu can talk to the launcher. This change builds the real saves on top of it.

## What Zach gets (plain language)
- **Saves belong to a character.** Each character has its own list of saves. Loading one rewinds only that character. Your other characters, the AI players, monsters, the auction house and other characters' mail are not touched.
- **A save holds that character's progress:** level and XP, talents and spells, gear, bags, bank, gold, quests, reputation, professions, pets and position (including the home inn you're bound to). It holds no AI players, no monsters (the server never stores live monster state anyway, only respawn timers) and no other character.
- **Three kinds of save:**
  - **Manual saves.** Esc → Saves → Save. You can give it a name; otherwise it gets one like "Elwynn Forest · Level 12 · Tue 21:04".
  - **One quicksave** per character, taken with the quicksave key. Each quicksave replaces the previous one.
  - **Autosaves** at safe moments: entering an inn or city, taking a flight, and arriving in a new zone or dungeon. The last few are kept per character.
- **No saving in combat** (nor while dead). The game says so, and nothing is written. An autosave that falls in combat is skipped.
- **Loading never closes the game:**
  - *In the game:* Esc → Saves → pick one → Load (or the quickload key). Your character goes straight to the character screen, comes back as saved, and you press Play.
  - *At the character screen:* select a character → Saves → pick one → Load. After a moment ("Loading your save…") the character list shows the restored character.
  - Before every load the current state is saved automatically ("Before loading"), so a load can be undone.
- **An alt used as a companion keeps what it gained on the trip**, even if your main reloads (Zach, 2026-10-09).
- **Whole-world backups stay exactly as they are** (auto on quit, Option-click Backups). They remain the safety net for the whole world.
- **Profiles come later.** Today's single world is the only profile. The menu is laid out so that profiles (each its own world) can sit above characters later without moving anything.
- **Two small fixes to the saves channel** (verifier follow-ups from PR #31):
  - A control request can no longer leave a stray name in the game's saved account name (`accountName` in `Config.wtf`).
  - The launcher's listener answers only the 3.3.5a (build 12340) client.

## Decisions this change follows (all Zach's, 2026-10-09; memory note "pause-and-saves-decisions-2026-10-09", STATUS)
- Elder Scrolls model: manual, quick and auto saves; loading doesn't touch other characters.
- Hierarchy: Profile (a world) → Characters → Saves. Profile-wide saves were rejected as confusing.
- Load at the character screen with no app or server restart. This reverses the 2026-10-06 "whole save only" choice for *saves*. Backups keep the whole save.
- No saving in combat. Quicksave and autosave happen only at safe moments.
- Ironman/hardcore is descoped. Character saves come first, profiles later.
- Whole-world backups stay as they are.

## What Changes
- **Server (mod-moonglade, no core patch):** a new `MoongladeSaves` part that writes a character's progress to a save file and loads it back in place, under the same character ID, while the server runs. It also runs autosave triggers and combat refusal, takes save and load requests from the in-game add-on (addon whisper, the same route `true-pause` uses), and adds a `.moonglade save …` command the launcher calls over SOAP.
- **Save store on disk** outside the live database: `<lane env>/saves/<ACCOUNT>/<character id>/<save id>/` (`env/saves/` for the player), with a manifest per save. It is never written into `client/` and never in a test run's player folder.
- **Launcher (`savesctl.py`, `play.py`, `frontmenu.py`, new `charsaves.py`):** a new `L` (load) verb on the control listener, the save list handed to the character screen, the `accountName` fix, and the build-12340 check.
- **Character screen (glue):** a Saves button and panel per character, with load, a confirm dialog and a "Loaded …" notice.
- **In-game add-on (`client-mods/addons/Moonglade`):** an Esc-menu Saves window (save, name, load, delete), quicksave and quickload key bindings, and the refusal messages.
- **Checks:** `saves.*` (server and probe, mostly full tier), the new `savesctl.*` checks, and GUI checks for the character screen.

## Out of scope
- Several profiles/worlds (their own change, later).
- Ironman/hardcore (descoped).
- Whole-world saves in the Skyrim style. These are only possible if client and server logic are ever consolidated (Zach's long-term note).
- Saving or loading AI players, companions' progress, or world state (monsters, auction house, instance lockouts).
- Moving saves between computers. The format is versioned so that this can come later.

## Open questions for Zach (plain language; defaults in brackets, the build proceeds with the defaults)
1. **Mailbox: part of the save or not?** If it is part of the save, loading also rewinds the character's mailbox: anything an alt mailed after that save disappears, and mail you already took comes back. [Default: **not** part of the save. The mailbox works like a shared post office between your characters, so loading never loses mail.]
2. **How many autosaves to keep per character?** [Default: 5. Plus 1 quicksave, 3 "before loading" safety saves, and as many manual saves as you like.]
3. **Quicksave/quickload keys.** [Default: F5 quicksave and F9 quickload, like Skyrim, if the game doesn't already use them; otherwise the closest free keys. Rebindable in Key Bindings.]
4. **Can quicksave be used anywhere outside combat, or only in "safe places"?** [Default: anywhere, unless you're in combat, dead, falling, in a battleground or arena, or in the middle of a zone change. Autosaves use the safe moments: inns and cities, flights, arriving in a zone.]
5. **A character's name and look:** if you renamed a character or changed their hair after a save, loading keeps the current name. The rest of the look rewinds to the save. [Default: as described.]

## Risks (designed around, see design.md)
- **Item duplication by mail or trade** (mail an item to an alt, then reload the main) is inherent to per-character saves in a single-player world. It is accepted and documented. It never corrupts the database: loaded items always get fresh IDs.
- **Getting the save list onto the character screen while the game runs** is not proven yet. Task 1.1 measures it first. If no live method works, the character screen lists saves known when the game started, and the in-game Saves window always has the full list. That would be recorded here as a spec change.

## Sources
- `docs/research/front-menu-saves-spike.md` on `spike/front-menu-saves` (B27): channel A1 and the token grammar.
- `openspec/specs/saves-channel/spec.md` and the archived change's design ("As built", "Live results", "Follow-ups").
- AzerothCore (pinned `core_sha` 7f12e89) `src/server/game/Tools/PlayerDump.cpp`: the core's own list of per-character tables and the ID columns that must be renumbered. design.md cites line ranges.
- Zach's decisions (2026-10-09), recorded in STATUS.md and the orchestrator's memory notes.

## Spec-change log
- 2026-10-10 (clarification, orchestrator): in-game `LOAD` (and `DELETE`) name a save by list generation + index from `MGSAVE LIST`, not by save id, so that no save id ever appears in reply lines (`saves.list_wording`). Same pattern as the glue's `L` verb. Found in builder run A.
- 2026-10-10 (wording): the in-game bind item's name, which matches the old project name, was replaced by "home inn" / "home bind" so tracked files pass `repo.naming`. No behaviour change.
- 2026-10-10 (default keys, per Zach's default "F5/F9 if free, else nearest free"): the client's own default bindings already use F5 and F9, so quicksave is F6 and quickload F7, set once only if still free.
