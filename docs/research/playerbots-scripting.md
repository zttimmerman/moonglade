# mod-playerbots scriptability for behavioral tests (task 8.1)

Builder B3, 2026-10-01. Non-blocking research for `docs/harness-requirements.md` item 6.

**Evidence status.** `harness/hs status` (run 2026-10-01) showed the stack down: no mysqld, authserver, or worldserver, and ports 3307/3724/8085/7878 all closed. The task rules don't let me start it, so **every finding below is from source reading** unless it is labeled otherwise. Nothing here was run against a live server. Source pins: `vendor/mod-playerbots` at `7bae1b5c` and `vendor/azerothcore-wotlk` (the pinned core). Paths are relative to the repo root. `server/etc/modules/playerbots.conf.dist` isn't installed yet, so config citations point to `vendor/mod-playerbots/conf/playerbots.conf.dist` (abbreviated **conf**).

**Summary.** Over SOAP the harness can create the random-bot population, teleport and level bots, give them quests, and read quest status. It cannot pick up or direct a specific bot over SOAP, because every `.playerbots bot …` command needs a logged-in session. A bot can be told to work one specific quest, through the chat command `rpg status do quest <id>` or `rpg do quest <id>`, or through config weights that leave "do quest" as the only RPG activity. There is no autonomous "run this dungeon" behavior. Dungeon strategies are per-boss tactics that switch on when a bot enters the map, and the core's LFG refuses to match groups made only of bots.

---

## 1. Commands usable without a logged-in human

### How SOAP commands are gated
- SOAP queues each request as a CLI command (`vendor/azerothcore-wotlk/src/server/apps/worldserver/ACSoap/ACSoap.cpp:121`), so the handler is a console handler.
- The command tree hides any `Console::No` command from a console invoker and shows every `Console::Yes` one (`vendor/azerothcore-wotlk/src/server/game/Chat/ChatCommands/ChatCommand.cpp:513-517`).

### `.playerbots` command table (`vendor/mod-playerbots/src/Script/PlayerbotCommandScript.cpp:24-46`)
| Command | Console? | Notes |
|---|---|---|
| `.playerbots bot …` (add, addaccount, addclass, init=*, remove, list, self, lookup, levelup, refresh, random, quests) | **No** (`:36`) | Also refuses without a session: "You may only add bots from an active session" (`vendor/mod-playerbots/src/Bot/PlayerbotMgr.cpp:872-877`). Every subcommand runs relative to `master` (`PlayerbotMgr.cpp:899-1330`, `ProcessBotCommand` at `:675-850`). |
| `.playerbots rndbot <sub>` | **Yes**, GM (`:39`) | Handler in `vendor/mod-playerbots/src/Bot/RandomPlayerbotMgr.cpp:2468-2588`. Subcommands: `reset` (wipes `playerbots_random_bots` and needs a restart, `:2484-2490`), `stats` (`:2492`), `reload` (re-reads config, `:2499-2503`), `update` (`:2505`), and these per-bot-name handlers: `init`, `clear`, `level`/`levelup`, `refresh`, `teleport`, `revive`, `grind`, `change_strategy` (`:2511-2521`). The `<sub> <name>` form matches `characters.name LIKE '<name>'` on random-bot accounts, and the default is `%`, meaning all bots (`:2529-2537`). |
| `.playerbots gtask`, `.playerbots pmon`, `.playerbots debug bg` | Yes (`:25,37,38`) | Guild tasks, performance monitor, BG debug. |
| `.playerbots account setKey/link/linkedAccounts/unlink` | No (`:29-32`) | Need a session. |

**Gotcha: `rndbot` output doesn't come back over SOAP.** The handler ignores its `ChatHandler*` (`RandomPlayerbotMgr.cpp:2468`) and writes everything with `LOG_INFO("playerbots", …)`, for example `:2478`, `:2560` and `:2572`. A SOAP reply will be empty or just "ok". The real output goes to `Playerbots.log` and the console appender (`vendor/azerothcore-wotlk/src/server/apps/worldserver/worldserver.conf.dist:679,713`).

### Core GM commands that work over the console and apply to bots by name
Bots are ordinary `Player` objects with world sessions, so core commands that take a `PlayerIdentifier` reach them:
- `.quest add|complete|remove|reward|status <quest> <name>`, all `Console::Yes` (`vendor/azerothcore-wotlk/src/server/scripts/Commands/cs_quest.cpp:39-43`). The name is a `PlayerIdentifier` (`:52`, `:751`). `.quest status` prints the live status and the reason a quest is unavailable (`:764-800`). `.quest add` rejects quests that start from an item (`:67-75`).
- `.tele name <name> <tele>` (`cs_tele.cpp:46,163`), `.tele name npc id|guid|name` (`:39-41`). Plain `.tele` is `Console::No` (`:54`).
- `.character level <name> <n>` (`cs_character.cpp:74`), `.revive` (`cs_misc.cpp:115`), `.pinfo` (`:135`), `.saveall` (`:123`), `.kick` (`:124`), `.additem` (`:130`).
- Need a session: `.summon`, `.appear`, `.groupsummon` (`cs_misc.cpp:110-112`), `.levelup` (`cs_character.cpp:83`).

### Commands that need a real player session
- All bot ownership: `.playerbots bot add/addclass/init/remove/self` (see above).
- All bot chat commands. The bot only receives commands through `OnPlayerCanUseChat` hooks, which fire when a **player** whispers the bot (`vendor/mod-playerbots/src/Script/Playerbots.cpp:227-250`), or chats in party (`:252-270`), guild (`:272-296`) or a channel (`:298-307`). Every one of those calls `botAI->HandleCommand(type, msg, player)` with a non-null `Player*`. The security check returns `DENY_ALL` when `from` has no session (`vendor/mod-playerbots/src/Mgr/Security/PlayerbotSecurity.cpp:21-27`). Queued commands are dropped when their owner is gone (`vendor/mod-playerbots/src/Bot/PlayerbotAI.cpp:579-584`).
- A GM-capable sender gets `PLAYERBOT_SECURITY_ALLOW_ALL` on **any** bot, random bots included (`PlayerbotSecurity.cpp:29-31`). `CanBeGameMaster` is an RBAC check on the session (`vendor/azerothcore-wotlk/src/server/game/Entities/Player/Player.cpp:2240-2243`). So a probe on a GM account can command any random bot without inviting it or becoming its master.

## 2. Directing a bot to a quest, travel, or dungeon

### Quests: yes
- The "new rpg" strategy is the default for random bots when `AiPlayerbot.EnableNewRpgStrategy = 1` (conf:1406). Ungrouped random bots, and random bots leading their group, get `grind` + `new rpg` (`vendor/mod-playerbots/src/Bot/Factory/AiFactory.cpp:593-632`). It is a state machine with the states IDLE, REST, WANDER_RANDOM, WANDER_NPC, GO_GRIND, GO_CAMP, DO_QUEST, TRAVEL_FLIGHT and OUTDOOR_PVP (`vendor/mod-playerbots/src/Ai/World/Rpg/Strategy/NewRpgStrategy.cpp:11-77`).
- **Chat commands that pin a quest:**
  - `rpg status do quest <id>`: the sender must be the bot's master or a GM (`vendor/mod-playerbots/src/Ai/World/Rpg/Action/NewRpgAction.cpp:59-69`). It parses the id (`:73-87`), requires the quest to be INCOMPLETE or COMPLETE in the bot's log (`:191-200`), then calls `ChangeToDoQuest` and whispers back `DO_QUEST <id>` (`:201-202`). With no id it picks the first log quest that has POI data (`:169-183`).
  - `rpg do quest <id>` / `StartRpgDoQuestAction` (`NewRpgAction.cpp:214-232`) switches without checking quest status and whispers "Start to do quest <id>".
  - Both are registered as chat triggers and actions (`vendor/mod-playerbots/src/Ai/Base/ChatTriggerContext.h:31-32`, `vendor/mod-playerbots/src/Ai/Base/ChatActionContext.h:109-110`).
  - `rpg status` with no argument whispers the current state (`NewRpgAction.cpp:51-57`). It is in the unsecured command list (`PlayerbotAI.cpp:941`).
  - The other states can be forced too: `idle`, `rest`, `wander random`, `wander npc`, `go grind`, `go camp`, `travel flight`, `outdoor pvp` (`NewRpgAction.cpp:92-166`).
- **What DO_QUEST does.** The bot moves to a random POI for an unfinished objective, using `quest_poi` through `sObjectMgr->GetQuestPOIVector` (`vendor/mod-playerbots/src/Ai/World/Rpg/Action/NewRpgBaseAction.cpp:832-848`). It relies on the `grind` strategy to kill and loot (`NewRpgAction.cpp:455-523`, comment at `:523`). When the quest completes it walks to the turn-in POI (`:574-615`). Accepting and turning in happen in `SearchQuestGiverAndAcceptOrReward` (`NewRpgAction.cpp:432`; accept and reward at `NewRpgBaseAction.cpp:322-346`).
- **Built-in timeouts:**
  - No progress after 5 minutes at a POI: the quest is marked low priority (abandoned) and the bot goes idle (`poiStayTime`, `vendor/mod-playerbots/src/Ai/World/Rpg/Action/NewRpgAction.h:112`; logic at `NewRpgAction.cpp:530-558`).
  - DO_QUEST lasting 30 minutes drops to IDLE (`statusDoQuestDuration`, `NewRpgAction.h:68`; `NewRpgAction.cpp:289-297`).
  - The turn-in POI also has the 5-minute timeout (`:624-633`).
- **The bot only quests on POI-backed quests.** A quest needs `quest_poi` rows for DO_QUEST to target it, both when forced (`NewRpgAction.cpp:487-493`) and when picked at random (`NewRpgBaseAction.cpp:1150-1176`).
- **Alt and addclass bots** aren't random bots. `IsRandomBot` requires a random-bot account (`RandomPlayerbotMgr.cpp:2221-2228`), so these bots get `follow` and `quest` but not `new rpg` (`AiFactory.cpp:586-587,672-673`). A master could add it at runtime with the `nc +new rpg,+grind,-follow` strategy command (`ChatTriggerContext.h:63`). Whether "new rpg" behaves correctly with a master attached is **unverified**: `NewRpgBaseAction.cpp:326,338` only reports to the master and doesn't block.

### Travel: partly
- `rpg status travel flight` sends the bot to a **random** level-appropriate flight path, not one you choose (`NewRpgAction.cpp:145-160`). `go grind` and `go camp` also pick random targets (`:117-144`).
- Deterministic placement goes through core GM commands instead (`.tele name <bot> …`, see §1), or the bot chat commands `teleport`, `summon` and `go` (`ChatTriggerContext.h:42,105,123`). Those need a session, and `summon` also needs a grouped master.
- Over SOAP, `.playerbots rndbot teleport|grind <name>` teleports a random bot to a random level-appropriate spot (`RandomPlayerbotMgr.cpp:2517,2520`).

### Dungeons: no autonomous mode
- Instance strategies such as `wotlk-uk` get applied when a bot is on a matching map (`PlayerbotAI.cpp:1636-1720`; context in `vendor/mod-playerbots/src/Ai/Dungeon/DungeonStrategyContext.h:50`). They contain boss mechanics for TBC/WotLK dungeons and raids only. The list at `PlayerbotAI.cpp:1638-1646` has no classic 5-man dungeon.
- No RPG state or chat command clears a dungeon. Bots in an instance follow and assist their master or leader.
- **LFG won't form a bot-only group.** The core asks `OnPlayerbotCheckLFGQueue` before creating a proposal (`vendor/azerothcore-wotlk/src/server/game/DungeonFinding/LFGQueue.cpp:415-418`). Playerbots returns true only when the queue holds a pre-made group, a real player, or a selfbot (`vendor/mod-playerbots/src/Script/Playerbots.cpp:435-451`). Random bots do queue themselves (`lfg` strategy when `RandomBotJoinLfg = 1`, `AiFactory.cpp:601-602`, conf:753).
- A dungeon behavioral test therefore needs a real session (the probe) in the group. That session can't fight intelligently unless it is turned into a selfbot (`.playerbots bot self`, GM-only at `AiPlayerbot.SelfBotLevel = 1`, conf:189-191; `PlayerbotMgr.cpp:1053-1072`). Selfbot plus addclass bots doing a dungeon is **unverified** and belongs to M4 research.

### Which driver to use
| Driver | Create bots | Pin a quest | Travel or dungeon | Verdict |
|---|---|---|---|---|
| (a) SOAP GM only | Random population only, through config at boot (conf:83-97); `rndbot init/level/teleport` | Indirect: `.quest add <id> <bot>` plus config where DO_QUEST is the only RPG weight (see (c)) | Teleport only | Workable for one quest test, with no chat feedback |
| (b) Probe as master/GM, whispering | `.playerbots bot addclass|add` typed as chat by the probe session | **Direct**: whisper `rpg status do quest <id>` to any bot (GM sender = ALLOW_ALL, `PlayerbotSecurity.cpp:29-31`); reply whispers confirm the state | Probe can group, summon and lead; needed for any dungeon or LFG test | Best fidelity; needs probe chat (CMSG_MESSAGECHAT send, SMSG_MESSAGECHAT parse) |
| (c) Config | n/a | `AiPlayerbot.RpgStatusProbWeight.*` (conf:1408-1426, parsed at `vendor/mod-playerbots/src/PlayerbotAIConfig.cpp:730+`); weight 0 disables a state (`NewRpgBaseAction.cpp:1089-1090`). Set every weight except `DoQuest` to 0 and an idle bot always picks a POI-backed quest from its log (`NewRpgBaseAction.cpp:1150-1176`) | n/a | Fine as a test profile; config applies at boot or after `rndbot reload` (`RandomPlayerbotMgr.cpp:2499-2503`, whether reload is safe at runtime is unverified) |

Two side channels:
- Bot strategies persist in `playerbots_db_store` (keys `co`, `nc`, `dead`, `value`). They are saved by `PlayerbotRepository::Save` (`vendor/mod-playerbots/src/Db/PlayerbotRepository.cpp:50-70`) and reapplied at login (`:28-41`, called from `PlayerbotMgr.cpp:530`). Writing an `nc` row over SQL while a bot is offline should set its strategies on its next login. This is **unverified**, and AiFactory defaults may be applied before or after the repository load.
- `CommandSeparator` is `\\` (conf:2559; split at `PlayerbotAI.cpp:966-976`), so one whisper can carry several commands.

## 3. Observable outcomes to assert on

| Signal | How | Caveat |
|---|---|---|
| Live quest state | SOAP `.quest status <id> <bot>` (`cs_quest.cpp:751-800`) | Best primary assertion: reads in-memory state, no save lag. |
| `characters.character_queststatus` / `character_queststatus_rewarded` | `hs sql characters …` | DB lags memory. `PlayerSaveInterval = 900000` (15 min) and `PlayerSave.AdditionalSaves = 0` (`worldserver.conf.dist:1805,1835`). Either run SOAP `.saveall` (`cs_misc.cpp:123`) before reading, or set `PlayerSave.AdditionalSaves` bit 2 (quest status, `:1822-1833`) in the test profile. |
| `characters.level/xp/map/zone/position_*` | SQL after `.saveall`, or SOAP `.pinfo <bot>` (`cs_misc.cpp:135,2058`) | Same save-lag caveat for SQL. |
| `playerbots.playerbots_random_bots` | SQL: which guids are random bots (`event='add'`), and the teleport/randomize/logout timers (`vendor/mod-playerbots/data/sql/playerbots/base/playerbots_random_bots.sql:2-14`; event names at `RandomPlayerbotMgr.cpp:767,1338,1450`) | Good for picking a bot and checking population counts. |
| `playerbots.playerbots_db_store` | SQL: the bot's saved strategies (`PlayerbotRepository.cpp:67-69`) | Written only when a strategy changes through chat (`vendor/mod-playerbots/src/Ai/Base/Actions/ChangeStrategyAction.cpp:24,28`) or on logout. |
| `Playerbots.log` | `Appender.Playerbots=2,5,0,Playerbots.log,w` and `Logger.playerbots=5` (`worldserver.conf.dist:679,713`). Level 5 is DEBUG (`vendor/azerothcore-wotlk/src/common/Logging/LogCommon.h:26-32`). | Grep for `[New RPG] <name> accept quest <id>` (`NewRpgBaseAction.cpp:333`), `[New RPG] <name> turned in quest <id>` (`:345`), `[New RPG] <name> marked as abandoned quest <id>` (`NewRpgAction.cpp:556,631`), and rndbot command output (`RandomPlayerbotMgr.cpp:2572`). The periodic stats dump, including the "Bots rpg status" counts, runs every `RandomBotPrintStatsInterval = 300` s (conf:1742; `RandomPlayerbotMgr.cpp:2912,2986-2990`). |
| Bot whisper replies (probe only) | SMSG_MESSAGECHAT whisper: "DO_QUEST <id>" status change (`NewRpgAction.cpp:202`; text keys from `vendor/mod-playerbots/data/sql/playerbots/updates/2026_05_08_00_ai_playerbot_rpg_status_texts.sql`), and "Quest accepted/rewarded" to the master (`NewRpgBaseAction.cpp:326-342`) | Needs probe chat parsing. Text is localizable through `ai_playerbot_texts`. |

## 4. Determinism, timing, and a recommended first test

**Main sources of flakiness (from source reading):**
1. **Activity gating.** By default only about 10% of bots are active when no real player is near (`AiPlayerbot.BotActiveAlone = 10`, conf:1273). SmartScale is on (conf:1311) and pauses more bots under load. Pathfinding (`DETAILED_MOVE_ACTIVITY`) only runs for force-active bots (`PlayerbotAI.cpp:4751-4754`). A bot counts as force-active when:
   - a real player is in the same zone or within 150 yd (`PlayerbotAI.cpp:4634-4677`, conf:1289-1290),
   - it has a real master (`:4679-4685`),
   - it is in combat or inside an instance (`:4612-4621`).
   The fix is either `BotActiveAlone = 100` with `botActiveAloneSmartScale = 0` (short-circuit at `PlayerbotAI.cpp:4609-4610`), or keeping a probe in the zone.
2. **Random bot drift.** Random bots get re-randomized and teleported on timers: `MinRandomBotTeleportInterval = 3600`, `Max = 18000` (conf:1751-1752; `RandomPlayerbotMgr.cpp:1562-1571`), and randomize every 2 h to 14 days (conf:1732-1733; `RandomPlayerbotMgr.cpp:1545-1550`). The first teleport is random and so are starting levels (conf:734-735,800-815). With `EnablePeriodicOnlineOffline = 0` (conf:758) bots stay logged in permanently (`RandomPlayerbotMgr.cpp:762-767`). A test has to neutralize this: teleport and level the bot over SOAP right before the test, and keep the test well under 1 h.
3. **Random choices inside DO_QUEST.** The POI is picked at random (`NewRpgAction.cpp:494`). Other quests in the log compete unless the bot is in forced mode, and it auto-accepts quests from nearby NPCs (`NewRpgBaseAction.cpp:322-334`). Mob competition with other bots and respawn timers also add variance. The bot can also die: `AiPlayerbot.BotCheats` defaults to `food,taxi,raid` (conf:633), and `health` is available.
4. **Hard ceilings.** A quest gets abandoned after 5 minutes at a POI without progress, and DO_QUEST gives up after 30 minutes (see §2).

**Timing estimate (not measured).** A one-objective starter-zone kill quest means walking a few hundred yards, about 8 to 10 kills, and walking back. I'd expect 5 to 15 minutes, with 30 minutes as the timeout because of the built-in ceilings. That belongs in a slow tier (nightly or `--behavioral`), not in `hs check` for M0. I'd guess flakiness at a few percent to tens of percent per run before tuning. The real number has to be measured: run the test N≥10 times and record the pass rate.

**Smallest viable behavioral test: `bots.quest_completion`**
1. **Test profile overrides** for playerbots.conf, rendered through `harness/hslib/config.py:61-67`, which today only injects the DB info:
   - `MinRandomBots = MaxRandomBots = 20`
   - `AddClassAccountPoolSize = 1` (or 0 for path A only)
   - `BotActiveAlone = 100`, `botActiveAloneSmartScale = 0`
   - every `RpgStatusProbWeight.*` = 0 except `DoQuest`
   - `BotCheats = "food,taxi,health"`
   - `RandomBotJoinLfg = 0`, `RandomBotJoinBG = 0`
   
   For worldserver.conf: `PlayerSave.AdditionalSaves = 2`.
2. **Pick the quest by SQL, not from memory** (CLAUDE.md §7). Choose an Alliance Northshire (zone 12) quest with one creature-kill objective, no item start, `quest_poi` rows present, and a quest giver with a fixed spawn: `hs sql world` over `quest_template`, `quest_poi`, `creature_queststarter` joined to `creature`. Record the chosen ID and the query in the test.
3. **Pick a bot by SQL.** Choose an online human or dwarf random bot from `playerbots_random_bots` (`event='add'`) joined to `characters` (race filter, `online=1`).
4. **Set it up over SOAP:**
   - `.character level <bot> <quest min level + 3>`
   - `.tele name <bot> <Northshire tele>` (pick the `game_tele` row by SQL)
   - `.quest remove <q> <bot>` for every other log quest, then `.quest add <id> <bot>`
5. **Drive the bot.** Path A, headless: just wait, because with the config in step 1 an idle bot will choose DO_QUEST for the only POI quest. Path B, probe: a GM probe character standing in Northshire whispers `rpg status do quest <id>` and asserts the `DO_QUEST <id>` reply.
6. **Assert.** Poll SOAP `.quest status <id> <bot>` every 30 s until it reads rewarded/complete, with a 30-minute timeout. Cross-check with a `Playerbots.log` grep for `turned in quest <id>`, then run `.saveall` and check `character_queststatus_rewarded`. Fail early when the log shows `marked as abandoned quest <id>`.

**Harness pieces needed:**
- **Path A:** the existing SOAP (`hs gm`) and SQL (`hs sql`) commands, a way to pass per-profile playerbots.conf overrides (`write_all` already has `extra_world`; it needs an equivalent for modules), a log-tail/grep helper for `Playerbots.log`, and a polling check with long timeouts in a slow tier.
- **Path B, in addition:** probe tasks 5.2 and 5.3 plus chat send/receive (CMSG_MESSAGECHAT whisper, SMSG_MESSAGECHAT parse) and a GM-level probe account. Path B is needed for anything involving groups, LFG, or dungeons.

## 5. Risks and knobs for a 16 GB Mac (M2 population, M4 dungeons)

- **The defaults are large, and the harness currently renders them unchanged.** `MinRandomBots = MaxRandomBots = 500` (conf:89-90). `AddClassAccountPoolSize = 50` creates 50 more accounts at about 10 characters each (conf:99-102; account math at `vendor/mod-playerbots/src/Bot/Factory/RandomPlayerbotFactory.cpp:385-420`). So the first `hs up` will create roughly 1000 bot characters and log in 500 of them. Recommendation for M0: override these to small values in `harness/hslib/config.py` (for example 20 random bots and AddClassAccountPoolSize 1–5). Raise them deliberately in M2 and measure RSS, tick time, and boot time with `.server info` and `pmon` (`PlayerbotCommandScript.cpp:66-101`). I have no memory-per-bot figure and am not guessing one.
- **Load knobs:**
  - `BotActiveAlone` and the SmartScale floor/ceiling at 50/200 ms tick time (conf:1273-1315)
  - `RandomBotsPerInterval = 60`, `RandomBotUpdateInterval = 20` s (conf:1716,1737): login and processing rate
  - `IterationsPerTick = 10`, `ReactDelay = 100`, `PassiveDelay = 10000` (conf:346,372,378)
  - `SightDistance = 100` (conf:401)
  - `EnablePeriodicOnlineOffline` with its ratio (conf:758-763) roughly doubles the stored population
  - `DisabledWithoutRealPlayer` (conf:117-119): bots log in only after a real player does. This is useful for the shipped game but breaks headless tests.
- **Population realism for M2:**
  - `RandomBotMinLevel/MaxLevel`, `SyncLevelWithPlayers`, `RandomBotConcentrateInPlayerZone` (conf:734-745)
  - `RandomBotMaps = 0,1,530,571` (conf:1573)
  - `AutoTeleportForLevel` (conf:1608)
  - `ZoneBracket.*` (conf:1428+)
  - The era profile's XP changes will interact with `RandomBotXPRate` (conf:823) and the level brackets.
- **M4 dungeons:** no bot-only LFG (`LFGQueue.cpp:415`, `Playerbots.cpp:435-451`). No classic-dungeon strategies (`PlayerbotAI.cpp:1638-1646`). Dungeon tests will need a session-holding leader, either the probe as a selfbot or the human. Watch `GroupInvitationPermission` (conf:142-144) and the gear-score and level checks in `PlayerbotSecurity.cpp:83-113`, though a GM sender bypasses them (`:29-31`). `MaxAddedBots = 40` (conf:136).
- **Chat-command fragility:**
  - `CommandPrefix = ""` (conf:2556): any whisper to a bot is parsed as a command.
  - Bot replies come from `ai_playerbot_texts` and can change between playerbots pins, so assert on log lines or GM queries rather than reply text where possible.
  - Whispering `logout` has a crash workaround (`Playerbots.cpp:243-247`). Avoid sending it from the probe.
- **First-boot cost:** account and character creation for the bot pool happens at worldserver start (`RandomPlayerbotFactory.cpp:324-420`). Expect a slower first `hs up`; the size hasn't been measured. `.playerbots rndbot reset` needs a restart (`RandomPlayerbotMgr.cpp:2484-2490`).

---

## Recommendation
1. **Feasible. Don't put it in M0.** A single-quest behavioral test is achievable with mechanisms the module already has (§2), with SOAP `.quest status` as the oracle and `Playerbots.log` as the trace (§3).
2. **Build path A first** (headless: config profile + SOAP + SQL + log grep), as `bots.quest_completion` in a slow tier with a 30-minute timeout. Measure the pass rate over 10 or more runs before trusting it. Move to path B (probe whisper `rpg status do quest <id>`) once probe tasks 5.2/5.3 land and chat is added. Path B is required for M4 dungeon and group tests.
3. **Action for M0, outside 8.1's scope (not done here, flagging for the orchestrator):** have `harness/hslib/config.py` override `MinRandomBots`, `MaxRandomBots` and `AddClassAccountPoolSize` to small values. The defaults (500 / 50 accounts) will make the first `hs up` slow and memory-heavy on a 16 GB Mac.
4. **Every claim above needs a live check** once the stack is up. The first things to confirm:
   - `hs gm ".playerbots bot list"` is refused or hidden over SOAP
   - `hs gm ".playerbots rndbot stats"` returns empty over SOAP while `Playerbots.log` gets the stats
   - `hs gm ".quest status <id> <rndbot>"` works on a bot
