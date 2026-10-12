# Design: m4-alt-companions

mod-playerbots pin `7bae1b5c`. `pb/` = `vendor/mod-playerbots/src/`, `conf` = `vendor/mod-playerbots/conf/playerbots.conf.dist`.

## 1. What exists
- **Adding an alt:** `.playerbots bot add <name>` goes to `PlayerbotHolder::ProcessBotCommand` (`pb/Bot/PlayerbotMgr.cpp:675-708`):
  - refuses a character that is already online;
  - allows the master's own account, or linked accounts when `AllowTrustedAccountBots` is set (:700-704: "you can only add bots from your own account or linked accounts");
  - `AllowAccountBots = 1` (conf:153-155) enables own-account alts;
  - `AllowGuildBots = 1` (conf:157-158) also allows guild members' characters. In single-player those can only be random bots in the player's guild, which is harmless and stays as is.
- **Dismissing:** `remove` → `LogoutPlayerBot(guid, true)`, which takes it out of the party (our `patches/playerbots/0001`, ADR-0008).
- **Commands an alt refuses:** `init=*`, `refresh` and the other admin commands return "You can only use this command on addclass bots" for alts (`PlayerbotMgr.cpp:733-738`). So an alt can't be re-rolled or re-levelled, which matches question 3's default.
- **Autogear:** the `autogear` and `autogear bis` chat commands refuse alts when `AutoGearCommandAltBots = 0`, but only for bots whose account isn't in the random-bot account list (`pb/Ai/Base/Actions/TrainerAction.cpp:578-584`, :322-328; `PlayerbotAIConfig::IsInRandomAccountList`, `PlayerbotAIConfig.cpp:944-947`).
  - That list is filled from the `rndbot<N>` accounts (`pb/Bot/Factory/RandomPlayerbotFactory.cpp:664-680`).
  - **Task 1.1 must confirm that the addclass pool accounts are in that list.** If they aren't, `AutoGearCommandAltBots = 0` would also block `/mg party`'s autogear for generated companions. The fallback is to keep the key at 1 and have the addon never send `autogear` to a non-addclass companion; `alt.gear_untouched`'s counterexample then becomes "the addon's command lists contain no autogear for alts".
- **Strategy persistence:** an alt's strategies persist in `playerbots_db_store` (F16), so `/mg bring` sends `reset botAI` after login.
- **Behaviour:** alts get `follow` + `quest`, not `new rpg` (`AiFactory.cpp:586-587, 672-673`). They fight with their spec's kit (see `m4-dungeon-companions` design.md §1).

## 2. Addon
- **`MoongladeParty.lua`** (shared with `m4-dungeon-companions`) gains `MG_BRING_PLAN`:
  - `/mg bring <name>` → `.playerbots bot add <name>`, wait for the party member, then whisper `reset botAI`;
  - `/mg dismiss <name>` → `.playerbots bot remove <name>`.
- **Never sent to an alt:** `autogear`, `talents`, `init`, `maintenance`. `/mg party` only whispers companions it created with `addclass`, tracked by name in the session.
- **If built first:** if this change lands before `m4-dungeon-companions`, `MoongladeParty.lua` starts here, with just the bring/dismiss table.

## 3. Saves interplay
- Front-menu-saves' rule: a load changes only that character's rows (character-saves spec, "SHALL NOT change any other character").
- `alt.progress_kept` loads a pre-trip save of the master and diffs the alt's rows (`characters`, `character_inventory`, `item_instance` owned by the alt, `character_queststatus`) before and after.
- Bot sessions never save on their own (character-saves spec). The alt's progress is written by the core's normal player save on logout or dismiss.

## 4. Checks
| Check | Tier |
|---|---|
| `alt.bring_dismiss`, `alt.foreign_refused`, `alt.progress_kept`, `alt.gear_untouched` | full, ~1–3 min each |
| ~~`client.bring_slash`~~ (cut 2026-10-12; now `unit:lua:test_addon_bring.*`) | unit |

- **Accounts:**
  - the probe uses the harness companion account (P.COMP_USER);
  - the alt is a second harness character on that account, named by a harness constant and created by the check;
  - `alt.foreign_refused` targets the HSTEST fixture account's character;
  - PLAYER's account and characters are never touched (builder checklist 9 and 11).
- **Counterexamples:**
  - ~~profile `altgear-on`~~ (cut 2026-10-12; a unit test checks every shipped profile sets `AutoGearCommandAltBots = 0`);
  - an alt on another account for `alt.foreign_refused` (the refusal is the pass condition; the counterexample adds the account link with `.playerbots account link` and shows the add succeeds, proving the check can tell the difference).

## As built (2026-10-12)

### Task 1.1 spike (live, m0 profile, mod-playerbots `7bae1b5c`)
- **Core premise holds.** With the master (Hscomp, HSCOMP account) in the world, `.playerbots bot add Hscompalt` (a second character on the same account) answers `add: Hscompalt - ok`; the alt is online and in the party within 0.6–1 s, its `characters.account` unchanged, and it whispers "Hi!". `AddPlayerBot` allows it as `sameAccount` (`PlayerbotMgr.cpp:101`, `AllowAccountBots = 1`).
- **Alts aren't re-levelled.** Only addclass bots are re-rolled to the master's level (`PlayerbotMgr.cpp:597-608`).
- **`reset botAI` right after the join can go unanswered.** In the first spike run the alt didn't answer within 10 s; later runs answered in about 3 s. The add-on and the check resend it every `MG_BRING_PLAN.resend` (5 s) until the answer, up to 30 s.
- **XP persists after dismiss.** The master killed a creature the comp.assists rule picked, with the alt summoned beside it: the alt's `characters.xp` went 0 → 93, saved when `.playerbots bot remove` logged it out (out of the party in 0.6 s, offline in under 1 s). Loot wasn't exercised: the master landed the kill with `.damage`, and nobody looted.
- **The addclass pool is in the random-account list.** The pool accounts are `RNDBOT1`–`RNDBOT5` (`playerbots_account_type.account_type = 2`). These are `AiPlayerbot.RandomBotAccountPrefix` accounts, which `RandomPlayerbotFactory.cpp:664-680` pushes into `randomBotAccounts`. So `AutoGearCommandAltBots = 0` keeps generated companions' autogear working (no §1 fallback needed). Confirmed live: in `alt.gear_untouched` the /mg party tank's `autogear` drew no refusal.
- **`autogear` on an alt with `AutoGearCommandAltBots = 0`:** refused. The answer is a system line to the master, `Hscompalt|cfff00000: You cannot use autogear on alt bots.` (`PlayerbotMgr::CheckTellErrors`), not a whisper. The alt's equipped items didn't change.
- **Another account's character:** `.playerbots bot add Hstester` answers `Failure: You are not allowed to control bot Hstester` (`AddPlayerBot`, `PlayerbotMgr.cpp:110-114`) and then `add: Hstester - ok` (`ProcessBotCommand` returns "ok" before the queued refusal). The character stays offline. The wording "you can only add bots from your own account or linked accounts" quoted in the spec is the `addaccount` path's (`:702-704`), not `add`'s. The check matches the `add` refusal.

### What was built
- **Add-on (`MoongladeParty.lua`):** `MG_BRING_PLAN` (`add`, `remove`, `steps`, `resend`), `/mg bring <name>` and `/mg dismiss <name>`.
  - The bring list: `.playerbots bot add <name>`, then, once the alt is in the party, the `reset botAI` whisper. The whisper can't come first, because a whisper to an offline character goes nowhere.
  - One bring at a time; a missing name prints the usage line and sends nothing.
  - `/mg party` never takes a brought alt (or one on its way) for one of its companions (`MoongladeBring_IsAlt`), so it never whispers an alt `autogear` or `talents`.
- **Config:** `AiPlayerbot.AutoGearCommandAltBots = 0` in `config.PLAYERBOTS`, so it's in every profile and in lane B's environment. The `altgear-on` profile was cut (spec-change log 2026-10-12).
- **Plan reader:** `partyplan.lua_table`, `bring_plan`, `bring_commands` and `alt_forbidden`, all fail-closed.
- **Checks (`harness/hslib/checks/alt_checks.py`):** one companion-probe session, `alt.bring_dismiss`, with children `alt.foreign_refused`, `alt.gear_untouched` and `alt.progress_kept`, so `alt.progress_kept` shares the login too.
  - The alt is `Hscompalt` (constant `ALT_CHAR`), created with `CMSG_CHAR_CREATE` on HSCOMP and set to level 8 (`companion.LEVEL`) while offline.
  - "/mg party runs alongside" is one /mg party companion (the damage list's first, the tank: addclass + its whisper steps with autogear), because `/mg party` itself refuses a non-empty party.
  - Kills are the master's `.damage` on creatures picked by the comp.assists rule, with the alt summoned beside it.
  - `alt.progress_kept` takes the master's save with `.moonglade save take` before the login, compares the alt's save-set rows (`saves.db_rows`, normalized) around `.moonglade save load` of that save, and clears HSCOMP's master folder in the lane's save store at the end.
- **Unit tests:** `unit:lua:test_addon_bring.*` (5), `unit:py:test_alt_config.alt_gear_protected_in_every_profile` (+3), and `tests/unit/test_alt_checks.py` (the pass rules' counterexamples, and `kill_one` against a fake server).
