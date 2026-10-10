## Design

Paths under `vendor/azerothcore-wotlk/` are the pinned core (`fork.lock` `core_sha` 7f12e89, the mod-playerbots fork, branch `Playerbot`). Line numbers are for that commit.

### 1. What a character's progress is (the save set)
The core already has a list of "everything that belongs to one character": the `pdump` tool's `DumpTables` (`src/server/game/Tools/PlayerDump.cpp:88-121`), with the ID columns that must be renumbered when a character is copied (`PlayerDump::InitializeTables`, `:253-376`). We start from that list, never from memory, and classify every table in it. A check (`saves.table_coverage`) also classifies every other table in `acore_characters` that has a character or item column, so a core bump that adds a table fails loudly.

**In the save** (from `DumpTables`; keyed by the character ID unless noted):

| Table | Holds | Renumbered on load |
|---|---|---|
| `characters` | level, XP, gold, position (map, x/y/z/o, taxi path), race/class/gender/look, play time, at_login flags, explored zones, titles, bank slots | — (the ID is kept, see 3) |
| `character_spell`, `character_talent`, `character_glyphs`, `character_action` | spells, recipes, talents (both specs), glyphs, action bars | — |
| `character_skills` | professions, weapon skills | — |
| `character_queststatus`, `_rewarded`, `_daily`, `_weekly`, `_monthly`, `_seasonal` | quests | — |
| `character_reputation` | reputation | — |
| `character_achievement`, `character_achievement_progress` | achievements | — |
| `character_aura`, `character_spell_cooldown` | buffs, cooldowns at save time | — |
| `character_homebind` | hearthstone location | — |
| `character_declinedname` | declined names (ruRU clients) | — |
| `character_inventory` | equipped items, bags, backpack, bank, keyring (`bag`, `item` are item IDs) | `bag`, `item` → new item IDs |
| `item_instance` | the items themselves. **Only items referenced by this save's `character_inventory`**. The core's dump takes every item whose `owner_guid` is the character (`BaseTables`, `:73-80`), which also catches items in auctions and mail. Those belong to the world and stay out | `guid` → new item ID |
| `character_gifts` | wrapped gifts (`item_guid`) | `item_guid` → new item ID |
| `character_equipmentsets` | equipment manager sets (`setguid`, `item0..18`) | `setguid` → new set ID, `itemN` → new item IDs |
| `character_pet`, `character_pet_declinedname` | pets and the hunter stable (`id` = pet number, `owner`) | `id` → new pet number |
| `pet_aura`, `pet_spell`, `pet_spell_cooldown` | pet buffs, spells and cooldowns (`guid` = pet number) | `guid` → new pet number |

**In the core's dump but not in our save:**
- `character_account_data`: per-character UI data (layout, macros). This is interface settings, not progress, and it would rewind the player's key bindings and macros.
- `mail`, `mail_items` (open question 1, default out): the mailbox works like a shared post office. Loading never deletes or resurrects mail.

**Not in either, deliberately left as is on load** (world or social state, or another character's):
- `character_social`, `guild_member`, `group_member`/`groups`, `arena_team_member`, `character_arena_stats`, `petition`/`petition_sign`, `calendar_*`.
- `character_instance` and `account_instance_times`: dungeon lockouts and resets are world state.
- `auctionhouse` and its items, `mail`.
- `character_battleground_random`, `character_entry_point`, `battleground_deserters`, `lfg_data`, `character_settings`, `character_brew_of_the_month`, `character_achievement_offline_updates`, `character_banned`.
- `item_refund_instance`, `item_soulbound_trade_data`, `item_loot_storage`: short-lived windows tied to old item IDs. They are dropped for items the load removes, because restored items get new IDs. This is documented: a vendor refund window doesn't survive a load.
- `character_stats` is derived, and the server rewrites it.
- `acore_playerbots`: the bots' own state, other characters.

**Removed on load:** `corpse` rows of the character. A save is never taken while dead, so the loaded character is alive, and an old corpse would be orphaned.

**Name.** The name stays the character's *current* name (open question 5). Every other `characters` column comes from the save.

### 2. How a save is stored
- **Where:** `<lane env>/saves/<ACCOUNT>/<character id>/<save id>/`. For the player that is `env/saves/` (a new `P.PLAYER_SAVES` next to `P.PLAYER_BACKUPS`). In release mode it is the app-data folder, the same rule as backups. Lane B writes only under its own lane root. The module gets the folder from `Moonglade.Saves.Dir` in `mod_moonglade.conf`, which `config.write_all` renders per lane.
- **Save id:** `YYYYMMDD-HHMMSS-<kind>[-n]`, where kind is `manual`, `quick`, `auto` or `before-load`. Ids never travel in a token (see 5); the menus show labels.
- **Files:**
  - `character.json.gz`: `{format: 1, schema: {table: [columns…]}, rows: {table: [[values…]…]}}`. Values are strings or null. Binary columns are hex. FLOAT/DOUBLE columns are read as `CAST(col AS DOUBLE)`, so positions are exact (the precision trap `harness/hslib/backups.py` documents: the text protocol gives 6 significant digits).
  - `manifest.json`: id, kind, label, created (UTC and local), account, character id, name, level, class, race, zone and area, map, play time, money, `core_sha`, the schema fingerprint (sha256 of the column lists), and the sha256 of `character.json.gz`. It never holds credentials.
- **Atomic:** the module writes into `.tmp-<id>/`, fsyncs, re-hashes, then renames to `<id>/`. Rotation runs only after the rename. Every entry point removes leftover `.tmp-*`, and the list never shows them. This mirrors `backups.py`.
- **Keep:** `auto` 5, `quick` 1, `before-load` 3, `manual` unlimited (open question 2), all per character. If the disk has less than 2 GB free (`backups.MIN_FREE_BYTES`), the save is refused with a message.
- **Writer:** the module writes, because it is the only party that knows when an online character's in-memory state has reached the database (see 4). The launcher only reads manifests and deletes on request.

### 3. How load works while the server runs
Load always happens with the character **out of the world**: at the character screen, or right after the in-game load has sent it there. It runs **inside the worldserver** (mod-moonglade), not as SQL from the launcher. The server keeps in-memory ID counters for items, pets, mail and equipment sets (`ObjectMgr::GetGenerator<HighGuid::Item>`, `GeneratePetNumber`, `GenerateEquipmentSetGuid`, `ObjectMgr.h:1145-1147`, `:1533-1537`). Rows inserted behind its back could collide with IDs it hands out later. The core's own `PlayerDumpReader::LoadDump` (`PlayerDump.cpp:764-966`) shows the pattern: take fresh IDs from those generators and advance them.

We don't call `LoadDump` itself, for three reasons:
- It never reuses an ID that's in use: it gives the copy a new character ID instead (`:772-783`), so it can't replace a character in place.
- It commits its own transaction (`:953`), which couldn't be combined with deleting the old rows (all-or-nothing).
- It loads every owned item, including auctions and mail (see 1).

Instead `MoongladeSaves` builds one transaction:
1. **Checks** (refuse with a reason, nothing changed):
   - The save exists, and its sha256 matches the manifest.
   - The manifest's account and character id match the target.
   - The character exists, isn't deleted, and isn't in the world: no `ObjectAccessor::FindConnectedPlayer`, which also covers a character logged in as a playerbot companion.
   - The save's schema fingerprint equals the running database's. A save from another core version is refused in v1, with "This save was made by a different version of Moonglade".
2. **Safety save:** a `before-load` save of the current state, written the same way as any save (see 4) and completed first.
3. **One transaction**, via `CharacterDatabase.AsyncCommitTransaction` (`DatabaseWorkerPool.cpp:281`):
   - Delete the character's rows in the save set. Items go by the character's `character_inventory` (`DELETE … WHERE guid IN (SELECT item FROM character_inventory WHERE guid = ?)` before the inventory itself), so auction and mail items are never touched. Pets go by `character_pet.owner`, sets by `guid`. Also delete `corpse` and the short-lived item tables for the removed items.
   - Insert the saved rows with the character ID kept and item, pet and equipment-set IDs replaced by fresh ones from the generators. The ID map is built from the dependent-column list in 1, mirroring `RegisterNewGuid`/`ChangeGuid` (`PlayerDump.cpp:493-516`).
   - Set `characters.name` to the current name and `online = 0`.
4. **Ordering.** Every earlier write of this character must land before the restore:
   - the last periodic save;
   - for the in-game path, the logout's pet save;
   - nothing after it: the in-game path logs out *without* the character save, `WorldSession::LogoutPlayer(false)` (`WorldSession.cpp:718`, the save is at `:844`).

   The character database's async queue is FIFO with one worker (`CharacterDatabase.WorkerThreads = 1`, the default in `worldserver.conf.dist:140`; `config.py` doesn't override it). So the restore transaction is queued after those writes and runs after them. The module refuses to enable saves if that option isn't 1 (logged; `saves.load_ordering` asserts it). The transaction's completion callback updates the character cache (`CharacterCache::UpdateCharacterLevel` and friends, `CharacterCache.h:52-56`) and records the outcome.
5. **All or nothing:** a failure inside the transaction rolls it back, and the character is exactly as before. A test switch (`HS_SAVES_TEST`, test account only) makes the transaction fail after N statements, so `saves.load_atomic` can prove it.

Keeping the character ID keeps everything that points at the character: friends, guild, group, companions' masters, mail, instance binds, and the front menu's "last played" line.

### 4. How a save is taken
- **Online character** (manual, quick, auto):
  - `Player::SaveToDB(trans, false, false)` (`Player.h:1605`) into a transaction, committed with `AsyncCommitTransaction`.
  - Then, on the same FIFO queue, a `CharacterDatabaseQueryHolder` with one SELECT per save-set table. With one worker, nothing interleaves between the commit and the reads, so the snapshot is consistent.
  - The holder's callback, on the world thread, serializes and writes the files (step 2). Writing ~100 KB–1 MB to disk is short, but it may move to a helper thread if `saves.manual` timing shows a hitch.
- **Offline character** (the before-load save at the character screen): the same holder, without the SaveToDB.
- **Who may save:** only a player with a real client session (`!WorldSession::IsBot()`, `WorldSession.h:1231`). Bots and companions never save.
- **Refused** (message to the player, nothing written):
  - in combat (`IsInCombat()`), dead or a ghost;
  - falling, or in a battleground or arena;
  - between maps (teleport pending).

  On a flight is allowed: the taxi path is in `characters`, and login resumes the flight.
- **Autosave triggers** (`PlayerScript` hooks, `ScriptDefines/PlayerScript.h`), each subject to the refusals above and to a minimum interval (`Moonglade.Saves.AutoMinIntervalS`, default 120 s); a trigger in combat is skipped, not queued:
  - entering a rest area: the resting flag's rising edge, checked in `OnPlayerUpdate` (`:300`) at most once a second;
  - taking a flight: the taxi start, `OnPlayerUpdate` seeing `IsInFlight()` rise;
  - arriving in a new map, including dungeons: `OnPlayerMapChanged` (`:383`).

### 5. Requests: who asks the server, and how
- **In the game (add-on → server):**
  - The add-on whispers itself an addon message with prefix `MGSAVE`, caught in `OnPlayerCanUseChat` (`PlayerScript.h:678-691`, the same route `true-pause` uses for `MGPAUSE`; whichever change merges second shares the parser).
  - Verbs: `LIST`, `SAVE manual <name>`, `QUICK`, `LOAD <save id>`, `QLOAD`, `DELETE <save id>`.
  - Replies are addon whispers to the player: one line per save (≤ 255 bytes; label, kind, time, level, zone) and one result line.
  - **In-game load:** the checks of 3 run, then the server sends the instant-logout response and `LogoutPlayer(false)`, which sends `SMSG_LOGOUT_COMPLETE` (`WorldSession.cpp:898`). The client goes to the character screen. The restore transaction is queued in the same world-thread step, so the client's character-list request (an async query too) is answered after it, with the restored character. This works in combat too: loading is how you undo a bad pull. Saving isn't allowed in combat.
- **At the character screen (glue → launcher → server):**
  - The glue sends `HS<nonce>L<g><nn>` over the proven channel (`savesctl`). `g` is a 1-hex-digit list generation, `nn` a 2-digit index into the list the glue was given. Tokens stay ≤ 16 chars `[A-Z0-9]` (spike, "Name limits").
  - The launcher maps (generation, index) to (character id, save id). It refuses a stale generation, then calls `.moonglade save load <character id> <save id>` over SOAP (`harness/hslib/soap.py`). The command queues the restore and returns a job id. The launcher polls `.moonglade save status <job>` (≤ 10 s).
  - Only then does the listener reply: ACCEPTED (0x08) for done, REFUSED (0x03) for refused or failed. The reason goes to `play-last.json` and the glue notice comes from the next list.
  - The listener handles one load at a time. A second one while busy gets REFUSED.
  - Afterwards the glue asks for a fresh character list (`GetCharacterListUpdate()`). The restored level and zone show without leaving the character screen.
  - Whether a control request can be sent *from the character screen* (it changes `realmList` while connected to the world) is unproven. Prototype Q2 measures it. Fallback: the glue goes back to the title screen behind the "Loading your save…" overlay (`DisconnectFromServer()`), sends the request, then logs in again through the existing Continue path (adds about 2 s). Either way the app and the server keep running.
- **savesctl verbs:** `VERBS` gains `L` with argument `[0-9A-F][0-9]{2}`. `Listener.actions["L"]` is the launcher callback above. It runs on the listener thread and is bounded by the 10 s poll, while the glue's `HS_CONTROL_TIMEOUT` rises to 15 s for `L` only.

### 6. Getting the save list onto the character screen
Today the glue's data comes from `HSProfiles.lua`, written once per launch into `patch-<locale>-5.MPQ` (`harness/hslib/frontmenu.py`). Saves made in the game during the session must appear at the character screen without a restart. Glue reloads its Lua after you leave the world: `HSFrontMenu.lua` notes that the glue screens start again at character select with fresh state. Whether it re-reads changed *data* then is unproven. Prototype Q1 tries these in order and keeps the first that works live:
- (a) **A fixed-size slot.** A second archive member, `Interface\GlueXML\HSSaves.lua`, stored uncompressed and padded to a fixed size (64 KiB) in `patch-<locale>-5.MPQ`. The launcher rewrites its bytes in place (same offset and length, so the archive's tables stay valid) whenever the save store changes. The glue reads it at reload.
- (b) **A loose file** `Interface\GlueXML\HSSaves.lua` in the play client, if the client reads loose glue files (it does read loose files under `Interface\AddOns`, `docs/research/login-screen-spike.md` run 3b).
- (c) **Fallback:** the launch-time list in `HSProfiles.lua`, plus "Saves made since you started the game are in the in-game Saves menu". If it comes to that, it is recorded as a spec change.

`HS_SAVES` carries per character `{name, level, class, saves = {{idx, label, kind, when}}}` and the generation. It never carries ids, paths or the nonce. The launcher refreshes it when the save store's mtime changes, with a poll of ≤ 1 s while the game runs.

### 7. Saves-channel follow-ups
- **`accountName`.** `HSControl_Send` saves `GetSavedAccountName()` before the request and calls `SetSavedAccountName(saved)` when it resolves (reply or timeout). `Config.wtf` is written at EnterWorld (spike, "Config.wtf flush"), so the check enters the world after control requests and reads `accountName`. The launcher's `clientcfg` start-time rewrite also replaces an `accountName` matching `^HS[0-9A-F]{6}` with the default profile's account, as a second guard.
- **Build check.** `_handle` refuses a challenge whose build isn't 12340 or whose version isn't 3.3.5 (`why = "wrong build"`, REFUSED, logged).

### 8. Menus (plain language, no "server", "realm" or "account")
- **Character screen:** a "Saves" button under the character list. It opens a panel for the selected character: newest first, with each save's label, kind ("Quicksave", "Autosave", "Before loading") and time. Load asks "Load '<label>'? <Name> goes back to <time>. Your other characters aren't affected." Afterwards a line says "Loaded '<label>'".
- **In the game:** a "Saves" button in the Esc menu opens a window with the same list, plus Save (with an optional name), Load and Delete (manual saves only). The quicksave and quickload bindings are in the add-on's `Bindings.xml` (open question 3). The default keys are chosen by reading the client's default bindings, never from memory. Refusals show as the standard red error text ("You can't save in combat.").

### 9. Checks and tiers
- The probe-driven checks (`saves.*` server side) run the probe client and SOAP on the test account's fixture characters (`harness/hslib/fixtures.py`), on lane A or B, full tier. Each restores into the test account only. The player's characters and `env/saves/<PLAYER>` are never touched (`saves.player_untouched`, quick tier, like `backups.player_untouched`).
- `saves.table_coverage` runs in the quick tier if it fits the budget, otherwise full.
- The GUI checks (`saves.live_charselect_*`, `savesctl.account_name_kept`) share one real-client session, like `savesctl.live_session`.
- New offline listener checks: `savesctl.build_refused` and `savesctl.load_verb`, each under 1 s.

### Prototype first (task 1.1)
Before the builder commits to 5 and 6, it measures on the test account, using a scratch play client:
- Q1: which of 6(a)/(b) shows a save made in the world at the character screen.
- Q2: whether a control request works from the character screen without leaving it.
- Q3: one in-game load end to end, done by hand with a minimal module build: does the character list after `SMSG_LOGOUT_COMPLETE` show the restored level?

If Q3 fails, the builder stops and reports with evidence. Q1 and Q2 have fallbacks.

### Prototype results (task 1.1, 2026-10-10, builder run A)
All three questions hold; no fallback is needed. Runs used the test account (HSTEST / Hstester) only, on lane A, with a
scratch play client (`env/check-play-saveproto`, dropped after each run). Evidence (git-ignored): `harness/out/fms-proto/`
(the two prototype scripts, `ev.json` and the screenshots of the last two real-client runs).

Prototype code (kept on the branch, replaced by tasks 2.2–2.4 and 4.1–4.2): `MoongladeSaves.cpp` with
`.moonglade save protosnap <name>` (remembers an online character's level, XP, money and position) and the add-on
whisper `MGSAVE\tLOAD`, handled on the next world tick (`WorldScript::OnUpdate`, not inside the chat handler, which
still uses the player after the hook returns): instant `SMSG_LOGOUT_RESPONSE`, `LogoutPlayer(false)`, then one
`CommitTransaction` writing the remembered values. Glue: `HSSaves.lua` (default in patch 0002, the live list in a
64 KiB uncompressed slot of `patch-<locale>-5.MPQ`, `frontmenu.rewrite_saves`), plus test-only hooks (a glue-load
counter in `accountList` as `HSRN<n>|`, `HS_TEST.charselect_back`, the acts `csrec`, `ifround:N:<act>`,
`roundgate:N`, and `HS_TEST.ctl_charselect`). Add-on: test mode `MoongladeTest.hook = "saveproto"` (sends
`MGSAVE LOAD` 6 s after the world UI is up).

- **Q3 (in-game load, end to end): holds.**
  - Probe, 4 rounds (`q3.py`): snapshot at level 1 in Northshire; then `.character level` to 4 and `.tele` to
    Stormwind; in rounds 0 and 2 `.saveall` first, so the database held level 4 at Stormwind before the load. After
    `MGSAVE LOAD`: `SMSG_LOGOUT_RESPONSE` (result 0, instant 1) and `SMSG_LOGOUT_COMPLETE` within 2–7 ms; the
    character list requested right after showed level 1 at the snapshot position (4/4); the database row equalled
    the snapshot (`online = 0`); logging in again placed the character at the snapshot position.
  - Real client, 3 runs (`q12.py`, last run `q12g`): the add-on's `MGSAVE LOAD` took the client straight to character
    select in the same frame (`PLAYER_LEAVING_WORLD` at 9.06 s, no countdown, no Lua call to Logout). In run `q12g`
    the server logged the logout at level 6 (`Logout Character:[Hstester] … Level: 6`) and the character screen
    right after it listed Hstester at level 3, the snapshot (`csrec:r2:…:Hstester=3`); the next world login was at
    level 3.
- **Q1 (save list at the character screen): (a) the fixed-size slot works; (b) the loose file doesn't.**
  - The glue screens start fresh each time the client comes back from the world: the load counter read 2, 3, 4 in
    rounds after the 1st, 2nd and 3rd world exits (a continued glue session would have kept its in-memory state and
    counted once).
  - Each fresh glue load re-read the slot: rewritten in place to `gen = 2` while the client ran (the archive open in
    the client), the next glue load showed `gen=2, src=slot` (runs `q12a`–`q12g`). In run `q12a` round 2 still read
    `gen = 0` because that glue load happened 1 s *before* the rewrite (the world session had ended at once, see the
    note below); every load after a rewrite saw the new data. So nothing is cached between loads.
  - A loose `Interface\GlueXML\HSSaves.lua` (`gen = 3`) in the play client was never read: rounds 3 and 4 still
    showed `gen=2` from the slot.
  - Consequence for 4.1: the glue sees a changed list only at its next load (game start, or back from the world).
    A change made while it sits at the character screen (for example the "Before loading" save a load there
    creates) shows after the next glue load; the glue itself knows which save it just loaded, for the notice.
    A rewrite racing a glue load could give a torn read; the list carries an end marker the glue checks
    (task 3.1/4.1).
- **Q2 (control request from the character screen without leaving it): holds.**
  - With `HSControl_Send` allowed at `charselect` (test switch `ctl_charselect`), `ctl:S` from the character
    screen: one challenge to the listener's port (`connection.log` `RESPONSE_CONNECTED … 127.0.0.1:<port>`), reply
    accepted (`KA10`) in 0.0–0.1 s, realmList restored, no stock dialog frame, and the screen stayed at `charselect`
    with the character list (every one of the 7 real-client runs, `q12a`–`q12g`).
  - The world connection survived: Play right after it entered the world (`Login Character:[Hstester]` 20 s
    later, runs `q12b`–`q12g`). The client logs the failed fake login as `LOGIN_STATE_FAILED … DISCONNECTED` for the
    listener connection only.
  - Caveat found in builder run A (`q32a`–`q32c`, 2026-10-10, with the real `L` verb): right after a *login*, the
    client keeps its login-server connection open for a while at the character screen (`connection.log`: `GRUNT:
    LOGIN_STATE_DISCONNECTED` 4 s, 13 s and 27 s after `COP_GET_CHARACTERS` in three runs). A control request sent
    before that never leaves the client (only an empty `OPEN_STATUS_DIALOG CANCEL`; the listener saw no connection)
    and ends in the 5 s timeout ("couldn't reach", class U). The same request sent after the disconnect was answered
    in 0.3 s (`q32c`: `L101` accepted, the load done, and Play entered the world at the saved level 3 instead of 6).
    Back from the world (Q2 above) the connection was already closed. So the glue (task 4.1) must treat U at the
    character screen as "not yet": retry the unchanged request until the login connection has closed (a retry is
    safe: nothing reached the launcher), with the overlay up, within an overall limit.
- **Note (not a saves issue):** without a hardware key press after the client starts, every world entry ended at
  once with a client `CMSG_LOGOUT_REQUEST` (the 3.3.5a idle logout measured against Mac uptime,
  `checks/clientinput.py`). The prototype then sent one Shift press (`clientinput.press_shift`), as the world-entering
  checks do. The saves GUI checks must do the same.

### As built so far (builder run A: tasks 2.1–2.4, 3.1–3.2)
Where the code differs from the plan above, and why. The full "As built" comes with task 5.3.
- **Snapshot reads (4):** not a `CharacterDatabaseQueryHolder` (a holder takes prepared statements only, and the core
  has none for these column lists). After `SaveToDB` the world thread queues one `AsyncQuery` per save-set table back
  to back on the same single FIFO worker, so the reads run right behind the commit with nothing of this character in
  between. Each read is `SELECT 1, <columns> FROM (SELECT 1) hs LEFT JOIN <table> t ON <where>`: the core returns an
  empty result both for "no rows" and for a failed query, and the join's marker row makes a failed read fail the
  whole save (fail closed).
- **Add-on verbs (5):** `LOAD` and `DELETE` take `<generation> <index>` from the last `LIST` (generation = 4 hex digits
  of the sha256 of the list's ids; index 1 = newest), not a save id, because the spec's reply lines must not carry
  save ids (`saves.list_wording`). A stale generation is refused ("The list of saves changed"). `LIST` lines:
  `E\t<gen>\t<n>\t<kind>\t<when>\t<level>\t<zone>\t<label>`, then `END\t<gen>\t<count>`; results `OK|NO\t<verb>\t<text>`.
- **Shared add-on parser (5):** `MoongladeAddon.{h,cpp}` owns `OnPlayerCanUseChat`; `MGPAUSE` and `MGSAVE` register by
  prefix. A load is handled on the next world tick (`WorldScript::OnUpdate`), never inside the chat handler.
- **Test switches (3, 9):** not an `HS_SAVES_TEST` environment variable but console-only GM commands, which the game
  can't reach and which apply to characters of the test account (`HSTEST`) only: `.moonglade save test failafter <n>
  | pause <s> | off`, and `.moonglade save interval <s>` (autosave interval until restart). The failing statement is
  a read that errors (`ER_SUBQUERY_NO_1_ROW`): a missing-table, unknown-column or parse error makes the core abort the
  worldserver (`MySQLConnection::_HandleMySQLErrno`).
- **Commands:** `.moonglade save status [job]`, `tables`, `take <guid> [label]`, `load <guid> <id>`, `list <guid>`,
  `interval`, `test`.
- **Ids:** `YYYYMMDD-HHMMSS-<kind>[-n]` in UTC; `n` is one more than the highest of that second and kind, so an id freed
  by rotation is never reused for a newer save.
- **Autosave labels:** `Rested: …`, `Took a flight: …`, `Arrived: …` (`<Zone> · Level <n> · <Ddd HH:MM>`); triggers are
  queued 1 s after the moment and skipped (logged, never queued again) inside the interval or when a save would be
  refused. The login map is not an arrival (5 s after login).
- **Listener (5):** connections are served on their own threads (at most 4) so a second `L` during a load is refused at
  once; `Listener.validators["L"]` refuses a stale or unknown list entry before the action runs.
- **HS_SAVES (6):** `{gen = "<hex>", chars = {{name, level, class, saves = {{idx, label, kind, when, level}}}}}` plus
  `HS_SAVES_END = "<hex>"` last; at most 20 saves per character and 99 in all on the character screen.
