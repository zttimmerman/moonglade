# Design: M2 Living world

## Context
See proposal.md and `docs/research/playerbots-scripting.md` (sections 1, 2, 4 and 5) for what mod-playerbots can do headlessly.

## Decisions
- **Profiles in config.py.** `profile="m0"` keeps 10 bots for fast tests. `profile="population"` sets N random bots plus level/zone distribution and activity knobs (e.g. `BotActiveAlone`, SmartScale, teleport intervals). The builder picks the exact keys from `playerbots.conf.dist` and documents each one. Checks that need population bring the stack up with that profile; the runner orders them so that one warm-up serves `pop.*` and `perf.*`.
- **Zone mapping.** Read `AreaTable.dbc` (WDBC reader exists in `tools/era-diff/era_diff.py`; move it into `harness/hslib/dbc.py`). Starting zones and capitals are identified from DBC/`game_tele` data, with the selection rule written in the check; never from memory.
- **Probe chat.** Add CMSG_MESSAGECHAT (say/party/whisper) and SMSG_MESSAGECHAT parsing to `harness/hslib/probe/`, with opcodes and layouts taken from AC source. Companion checks run the probe as a GM-level account (playerbots grants GM senders full control).
- **Moving the master.** A GM teleport of the test character (`.tele name`) stands in for walking. Follow is measured by bot position (the `.pinfo`/`.gps`-style output over SOAP, or the probe's received movement updates; the builder chooses and documents).
- **World-update time.** Use the server's own metric: `.server info` reports update-time diffs in AC. If it doesn't expose a p95, sample it every 10 s and compute the statistics over the window.
- **Tuning record.** `docs/research/m2-population-tuning.md`: one row per measured N with peak and median metrics, picking the largest N within the budget.

## Notes from later work (2026-10-02)
- Upstream server-mail login crash is fixed by core patch 0001 (ADR-0006); bots logging in and out are safe from it.
- `fixtures.settle_login` requires `CharacterDatabase.WorkerThreads = 1`; if the population needs more DB workers, remove settle_login (core patch makes it non-essential) rather than weaken its barrier.
- Keep the Mac awake during long runs (F12): wrap warm-up/sampling in `caffeinate` or an IOPM assertion; a mysqld stall was seen during display sleep.
- Tag population/perf checks `slow`; companion checks can be `full` if they take < ~2 min.

## Risks
- Warm-up time grows full runs by about 10 minutes. → Acceptable; one shared warm-up.
- Bot behaviour is stochastic. → Thresholds use generous percentages and evidence keeps the raw samples; flakiness counts as P0, so tune the thresholds or knobs with recorded reasons.
- 16 GB is tight with the client running. → The budget reserves 3 GB host memory for the client; the human scenario confirms it.

## Builder notes, tasks 1.0 / 1.1 / 2.1 / 4.1 (B12, 2026-10-03)

### Slow-tier rule (1.0)
`runner.slow_tier_skips` lists the slow checks referenced by a gating scenario (archived specs + `harness/active-changes`). A **full**-tier run records each as `skip` with the message `slow tier, not run in a full run` and lists them in `meta.slow_tier_skipped`. `runner.tolerated_skip` accepts such a record only when the run's tier is `full`, the registry says the check is `slow`, the runner listed it, and the message matches. `reporting.full_run_record` leaves those records out of `non_passing`. A slow run executes them like any check. Any other skip still fails. `report.slow_tier_rule` (quick) drives the runner's own functions (`select`, `slow_tier_skips`, `execute`, `full_run_record`, `run_ok`) over a synthetic registry, covering five cases: a full run, a slow run that passes, a slow run that fails, a full check that skips, and a full check that imitates the slow-skip message.

### Profiles and helpers (1.1)
- `harness/hslib/config.py` `PROFILES`: `m0` (10 random bots, the default) and `population`. Select one with `hs up --profile population`, `HS_PROFILE=population` (also read by `./play` and `hs launch`, since they call `stack.up`), or `stack.up(profile=…)` in code. A check declares `@check(..., profile=…)`. `stack.up` records the running profile and a digest of its overrides in `env/run/stack-profile.json`. The runner restarts a worldserver that runs a different profile (`stack.ensure_profile`). `hs up --profile X` refuses when a worldserver already runs with another profile.
- `HS_POPULATION_N=<n>` renders the population profile at another N. It is for tuning runs only: `perf.population_tuned` fails a session whose N isn't `config.POPULATION_N`.
- `harness/hslib/botlog.py`: `offset/lines/grep/count` over `env/logs/Playerbots.log`, which is truncated at each worldserver start.
- `harness/hslib/dbc.py`: the WDBC reader moved from era-diff (era-diff imports it), plus `area_table()` and `top_zone()`.
- `util.keep_awake`: `caffeinate -i -d -u -w <pid>` around every `hs run` and every population session (F12). The session evidence includes the `pmset -g assertions` lines for that pid.
- mysqld (`stack._write_my_cnf`, every profile): `performance_schema=OFF` and `innodb_log_buffer_size=16M`. Measured with the M0 profile, mysqld's memory footprint dropped from 1.01 GB to 0.70 GB, under the 1.0 GB budget. Nothing reads performance_schema.

### Population profile knobs (2.1)
Line numbers refer to `server/etc/modules/playerbots.conf.dist` (pinned playerbots `7bae1b5c`) and `server/etc/worldserver.conf.dist`.

| Key (line) | Default | Population | Why |
|---|---|---|---|
| `MinRandomBots` / `MaxRandomBots` (89-90) | 500 | `POPULATION_N` = 300 | Measured choice, see `docs/research/m2-population-tuning.md`. Min = Max fixes the count, so `bot_count` never re-rolls (`RandomPlayerbotMgr.cpp:300-309`). |
| `AddClassAccountPoolSize` (102) | 50 | 5 | Same as m0, so no extra accounts get created. The companions pool belongs to task 3.x. |
| `RandomBotMinLevelChance` (807) | 0.1 | 0.4 | On a re-roll, 40% of bots come out at level 1. Bots at level 4 or below are only placed within 500 yd of their race's start position (`PlayerbotAI.cpp:6497-6523` `StarterLevelDistanceCheck`), so they fill the 8 starting zones. |
| `RandomBotMaxLevelChance` (811) | 0.1 | 0.05 | Level-80 bots only go to Dalaran or Northrend. Lowering this leaves more bots in levels 10-60, which use the base capitals. |
| `ProbTeleToBankers` (1577) | 0.25 | 1.0 | Every "teleport for level" by a bot of level 10 or higher goes to a capital banker (`RandomPlayerbotMgr.cpp:1803`; `urand(0,100) < 100` is true 100 times in 101). Levels 10-60 go to their faction's 4 base capitals, 61-70 to Shattrath, 71+ to Dalaran (`TravelMgr.cpp:4789-4803`). From there the RPG strategy sends them out by flight, grinding and quests, so capitals act as hubs and the leveling zones fill as bots fly out. |
| `TeleTo<City>Weight` (1582-1589) | SW 2, Org 2, others 1 | SW 1, Org 1, IF 2, UC 2, TB 2, others 1 | These weights choose which capital a city-bound bot goes to. They were balanced from measured capital counts after fresh starts (table below). The default doubles Stormwind and Orgrimmar, and Thunder Bluff then held 1-2 bots. The final setting is the best one measured. Shattrath and Dalaran are untouched, since only 61-70 and 71+ bots use them. |
| `Min/MaxRandomBotTeleportInterval` (1751-1752) | 3600 / 18000 | 300 / 1200 | Capitals drain within minutes of the boot burst (Ironforge went from 25 to 3 bots in 8 minutes). Every 5-20 min each idle bot teleports for its level again, which for level 10+ means to a capital. Capitals keep refilling, and bots come back to town the way players do. Teleports never happen within 150 yd of a real player (`RandomPlayerbotMgr.cpp:1708`). |
| `BotActiveAlone` (1273) / `botActiveAloneSmartScale` (1311) | 10 / 1 | 100 / 0 | Every bot acts with no real player nearby. Otherwise about 90% sit idle (research doc §4, short-circuit at `PlayerbotAI.cpp:4609-4610`). Measured cost: world update p95 of 18 ms or less at N=300. |
| `RandomBotJoinLfg` (753) / `RandomBotJoinBG` (1619) | 1 / 1 | 0 / 0 | Queued or instanced bots leave the open world, which is the thing this profile exists to populate. LFG can't form bot-only groups anyway (`LFGQueue.cpp:415-418`, `Playerbots.cpp:435-451`). Revisit at the BG and dungeon milestones. |
| relied on, unchanged | | | `EnablePeriodicOnlineOffline = 0` (758) keeps the online set fixed. `DisabledWithoutRealPlayer = 0` (117) lets bots run headless. `RandomBotMaps = 0,1,530,571` (1573) covers all four continents. `EnableNewRpgStrategy = 1` (1406). |
| `MapUpdate.Threads` (wsconf 1334) | 1 | 4 | Bots spread over 4 continent maps, and the M2 Pro has spare cores. |
| `DontCacheRandomMovementPaths` (wsconf 1424) | 0 | 1 | Trades CPU (which had headroom) for memory (which is the constraint). |
| `Appender.Playerbots` (wsconf 679) | flags 0 | flags 3 | Adds a timestamp and level prefix (`LogCommon.h:53-55`), so `pop.size_stable` can see FATAL lines. |
| `Logger.playerbots` (wsconf 713) | 5 | 4 | Drops DEBUG output. With 10 bots, startup alone wrote 300 KB/min at DEBUG. |

Capital counts (online random bots per capital, from `.saveall` and `characters.zone`, N=300, fresh start) at seconds after `hs up`:

| Run | Weights / teleport interval | t | UC | SW | IF | Org | TB | Darn | SMC | Exo |
|---|---|---|---|---|---|---|---|---|---|---|
| slow run 1 | all 1 / 3600-18000 | 571 | 6 | 5 | 6 | 10 | **2** | 9 | 12 | 6 |
| R1 | all 1 / 3600-18000 | 90 / 570 | 5 / 4 | 9 / 8 | 6 / 3 | 13 / 7 | 5 / 4 | 13 / 15 | 14 / 9 | 12 / 9 |
| R2 | IF, UC, TB 2 / 3600-18000 | 90 / 570 | 15 / 12 | 12 / 3 | 25 / 3 | 6 / 8 | 16 / 13 | 6 / 14 | 10 / 3 | 4 / 11 |
| R3 (final) | IF, UC, TB 2 / 300-1200 | 90 / 570 / 900 | 19 / 6 / 9 | 7 / 4 / 3 | 15 / 6 / 9 | 10 / 7 / 7 | 18 / 11 / 9 | 10 / 10 / 9 | 9 / 5 / 6 | 10 / 7 / 6 |
| R4 | + SW, SMC 2 / 300-1200 | 300 / 570 | 5 / 4 | 4 / 4 | 4 / **0** | 3 / **1** | 13 / 7 | 4 / 6 | 11 / 9 | 8 / 6 |

Single runs vary a lot. Bots reach a capital in a burst at boot and leave within minutes by flight, grinding or quests. A capital's count at any moment is roughly inflow × residence time, and at N=300 its minimum swings between 0 and 4. See "Open: capital coverage" below.

**Fresh world at every population start** (`population.fresh_start`, called by `stack.up` before the worldserver starts, population profile only). mod-playerbots stores each bot's level and its next randomize and teleport times (teleport every 1-5 h, randomize every 2 h to 14 d). Without a reset, each boot resumes bots where the last session left them. Measured: a second session at N=250 had 0-2 bots in 7 of the 8 capitals, because bots had walked out and their teleport timers hadn't expired. Over accumulated play, bots also level out of the starting zones. The step sets random-bot characters to level 1 and deletes their `randomize`/`teleport` timer rows. Seconds after login, `Randomize()` then takes the `RandomizeFirst` path: a new level from the knobs above, then a teleport for that level. In gameplay terms, every time the server starts the ambient bots re-roll like a fresh realm, while names, races and classes persist. It touches only characters on accounts with `playerbots_account_type = 1`. `pop.size_stable` compares checksums of non-bot accounts and characters before and after.

### Zone targets (data rule, never memory)
- **Starting zones:** for each race in `world.playercreateinfo`, take the start zone shared by most of its classes (this outvotes the Death Knight start), then map it to its top-level zone through `AreaTable.dbc` parents. Races that share a zone count once.
- **Capitals:** `AreaTable.dbc` zones (parent 0) with `AREA_FLAG_CAPITAL` 0x100 (`DBCEnums.h:242`) and team ALLY (2) or HORDE (4) (`DBCEnums.h:56-59`). This drops the neutral, capital-flagged Shattrath and Dalaran (team 6).
- A bot's zone is `characters.zone` after `.saveall` (the zone the server computed from its position), mapped to its top-level zone through `AreaTable.dbc`. The `.saveall` barrier: every online bot's `totaltime` (played seconds, written by `Player::SaveToDB`) must advance.

### Session and timing
`population.run_session`: stack down → `hs up --profile population` (T0) → 570 s warm-up → S0 (its `.saveall` must start within 600 s of T0) → S1 300 s later. Meanwhile a sampler thread takes one sample every 10 s for 600 s. All five checks read the same session (`ctx["population_session"]`). They run at order 400+, after every m0 check.

### Process memory metric
The budget's "resident memory" is measured as **max(ps RSS, `proc_pid_rusage` phys_footprint)**, which is the conservative reading. This Mac runs with 7-8.5 GB of swap in use, because of other apps. macOS compresses and swaps idle server heap, so RSS reads far below real use. Measured: worldserver RSS 0.75 GB against a footprint of 3.30 GB, with `vmmap` showing 3.0 GB allocated at 0% fragmentation. Footprint is the "Memory" column in Activity Monitor. Both figures are in every sample.

### Open: capital coverage and the budget (for the orchestrator)
- **Capital coverage at N=300 is marginal.** Starting zones hold with margin: 7 to 31 bots each in every measured fresh-start run. The 8 capitals, though, hold a minimum of 0-4 bots at the ~570 s snapshot depending on the run (table above). The knobs that raise it are more bots (`N`, which costs memory), a longer stay (RPG status weights, which would change bot behaviour everywhere) or faster refill (a shorter teleport interval, which pulls bots out of the leveling zones). None was taken further, per the orchestrator's instruction to stop at the budget conflict.
- **The budget conflicts with coverage.** Worldserver memory (max of RSS and footprint) is above 4.0 GB at every measured N, including N=50 (4.21 GB). The full table is in `docs/research/m2-population-tuning.md`. Host free+inactive memory is set by the other apps on this Mac.

## Builder notes, task 2.2 (B12, 2026-10-03): world-size setting, ~500 default, revised budget
- **Setting.** `harness/hslib/settings.py` reads `env/settings.json` (env/ is gitignored), or `$HS_SETTINGS` when set: `{"world_size": "small" | "normal" | "large" | <bots>}`. A missing file or key means `normal`. Change it with `harness/hs world-size small` (no argument shows the current value), or by editing the file. Presets live in `config.WORLD_SIZES`: small 150, normal 500, large 1000. The measurements behind them are in `docs/research/m2-population-tuning.md`. A numeric size must be 10-2000.
- **`./play`** (`play.py` → `settings.play_world()` → `launch.prepare(profile, population_n)` → `stack.up`) starts the population profile at the configured size. An explicit `$HS_PROFILE` wins, and then the settings file isn't read. `stack.up(population_n=…)` renders N, and `env/run/stack-profile.json` records `n` and a digest that covers N. `hs launch` and plain `hs up` still default to m0.
- **Checks never read the user's setting.** `play_checks` run `./play` with `HS_PROFILE=m0` pinned. The population checks use `config.POPULATION_N` (= the normal preset). `pop.size_setting` uses a scratch settings file through `$HS_SETTINGS`.
- **`pop.size_setting`** (slow, order 395, its own short boot before the main session). With no settings file, `play_world()` must resolve to population at normal. With a scratch file set to `small`, it calls the same `settings.play_world()` → `stack.up(profile, population_n)` path as `./play`. It then requires: a running profile of population with `n`=150 and a matching digest; online random bots within 5% of 150 for 3 consecutive polls 10 s apart, within 420 s. Login took about 1-2 min. The check adds about 3 minutes to a slow run.
- **`pop.fresh_start_scope`** (slow, shares the main session). `fresh_start` now records the non-bot checksums before and after its own SQL, the bot level histogram after the reset, and the randomize/teleport timer rows left. The check requires: all random-bot characters (at least N) at `FRESH_LEVEL` 1; 0 timer rows left; identical non-bot checksums across the reset and across the whole session. On the first boot after raising N, the new bot characters are created after the reset (they start at level 1 anyway), so `reset < N` fails that one run. The slow run never hits this, because the 1500-character pool already exists.
- **Budget** (`population.LIMITS`): worldserver 6.0 GB, mysqld 1.0 GB, stack total (worldserver + mysqld + authserver) 7.0 GB, p95 100 ms, median 50 ms. Host free+inactive is in `RECORDED`: summarized in evidence but not limited.
- **Capital weights at N=500.** With weight 1, Stormwind, Orgrimmar and Silvermoon held 5, 5 and 6 bots, against 13-15 for the weight-2 cities and 12-15 for Darnassus and Exodar. Final setting: every base capital at weight 2 except Darnassus and Exodar (1). Capital minimums at about 570 s: 12 at N=750; the N=500 numbers are in the report and the tuning record.
