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
