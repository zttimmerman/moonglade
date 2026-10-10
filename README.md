# Moonglade

Moonglade is single-player, fully offline World of Warcraft: **Wrath of the Lich King 3.3.5a (build 12340)** on an Apple Silicon Mac. It runs a local **AzerothCore** server with **mod-playerbots**, so about 500 AI players fill the world, and you can add bot companions to your party. An optional **vanilla mode** gives levels 1–60 the pre-2.3 pace and difficulty, and all WotLK content stays in place: Outland, Northrend and Death Knights. The game's own login screen becomes a **front menu**, so you press Continue and you're in. One double-click (or `./play`) starts the server and the game. Quitting stops everything.

It's a personal project. The source is public under the GPL (GPL-2.0-or-later), and no client or game files are distributed. **The WoW client isn't included.** Bring your own Windows 3.3.5a enUS client.

## Status

**Works today** (as of 2026-10-05):
- **M0 foundation:** the pinned server builds and boots natively on macOS ARM64, with the test harness complete. The Windows client runs under Wine.
- **M1 offline loop:** `./play` takes you from nothing running to in-game and back to nothing running, with no network needed.
- **M2 living world:** about 500 AI players across starting zones and capitals, plus companions you add to your party who follow you, fight with you and take shared quests. It stays within a measured memory budget on a 16 GB Mac.
- **M3 vanilla mode:** levels 1–60 use pre-2.3 XP, quest XP, outdoor elites and mounts. It's on by default, and all values come from VMaNGOS data.
- **Moonglade.app:** a double-clickable launcher for `./play`.
- **In-client login screen:** the Continue / Profiles / Settings / Extras menu inside the game's own login screen, running on a patched, revertable copy of the client.

**Next:** the **seamless experience** change (no server/realm/connecting wording on screen, plus a Saves entry in the in-game front menu), then **M4 solo dungeons**. Zach still has to play the "human-verify" scenarios (M2 living world and companions, M3 vanilla mode, the in-client login screen). The live roadmap, task board and known issues are in **[STATUS.md](STATUS.md)**.

## Requirements

- An **Apple Silicon Mac** with **macOS 26.2 or newer** (the pinned Wine runtime is built for it). It's developed on an M2 Pro with 16 GB RAM running macOS 26.6.
- About **28 GB of disk** for a full install (18 GB of it is your client; the play client is a copy-on-write clone, so it costs almost nothing extra), plus headroom: bootstrap wants about 35 GB free for a first run.
- **Homebrew** at `/opt/homebrew`. Bootstrap checks for the **Xcode Command Line Tools** and **Rosetta 2** and tells you the command if one is missing, and installs the Homebrew packages in the `Brewfile` with your OK.
- Your own **WoW 3.3.5a (12340) enUS Windows client**, as a zip or a folder.

[docs/setup.md](docs/setup.md) has what each setup step does, the measured times, a per-folder disk breakdown and the exact versions.

## Quick start

```sh
# 1. Homebrew (https://brew.sh), if you don't have it yet
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
eval "$(/opt/homebrew/bin/brew shellenv)"     # add this line to ~/.zprofile to keep it

# 2. clone
git clone https://github.com/zttimmerman/moonglade.git
cd moonglade

# 3. set everything up with your own client (a .zip or a folder)
./bootstrap ~/Downloads/YourClient_3.3.5a.zip

# 4. play: open dist/Moonglade.app (double-click, or drag it to the Dock), or
./play
```

`./bootstrap` checks the Mac and your client first and changes nothing until those pass. Then it installs the Homebrew packages it needs (it asks first; `--yes` skips the question), builds the pinned server, downloads the pinned server data and Wine runtime (each checked against its SHA-256 in `fork.lock`), creates the databases, installs your client (only reading your copy), builds the play client and the app, and sets [Moonglade's defaults](docs/setup.md#moonglades-defaults) (vanilla mode on, the normal world size, the front menu). The first run took 36 minutes on the dev Mac (M2 Pro), most of it building the server. Run it again any time: finished steps are skipped (a re-run takes seconds), and an interrupted run continues where it stopped. If a step fails, it says which one, why, and what to do.

**Chargers and the battery pause.** A laptop on a small charger (the stock 67 W one, for example) can lose charge even while plugged in during the server build. So the build uses only the Mac's performance cores (`--jobs N` changes that), and setup, builds and long harness runs pause with one line when the battery drops below 35 %, plugged in or not, and continue by themselves at 50 %. If the battery still falls to 15 %, the work stops cleanly; run the same command again later and it continues from that step. `--ignore-power` turns the pause off.

**First run of the game:** Moonglade creates your personal account `PLAYER` with a random password (not a GM) and saves it in `env/player.json`. That file is gitignored and readable only by you. To pick your own name and password, create that file before the first run (see docs/setup.md, "First run").

## Playing

### Moonglade.app or `./play`

- **`dist/Moonglade.app`** (build it with `harness/hs app`). Double-click it, or drag it to `/Applications` or the Dock, and it still finds the repo. It shows a small "starting" window, then the game. Quitting the game (or quitting the app from the Dock) shuts everything down. If something fails, a dialog tells you what went wrong and points you to the log.
  - **First launch:** macOS asks whether Moonglade may access your **Documents folder**, because the repo lives in `~/Documents`. Allow it. The permission is tied to the app's bundle id `local.moonglade.app`, so it survives rebuilds. Don't change the bundle id, or macOS will ask again.
  - The Dock shows two icons today: Moonglade, and Wine's process for the game. Clicking Moonglade says "already running", and the Wine icon brings the game forward. A fix for this is accepted (ADR-0010) and is part of the upcoming launcher polish.
- **`./play`** in a terminal does the same thing and prints its progress. The terminal stays busy while you play. Ctrl-C stops everything cleanly. Exit codes: `0` normal quit, `1` startup problem, `2` already running, `3` the client crashed, `129`/`130`/`143` SIGHUP/Ctrl-C/SIGTERM.

Both start the database, the login server and the world server, then wait until they're ready, make sure your account exists and open the game. With the ~500-bot world, startup takes a while.

### The front menu (the game's login screen)

With the default `patched` client, the login screen is Moonglade's menu. There are no username or password boxes:
- **Continue** (preselected) logs you straight in to character select. The line under it shows your last-played character, for example "Arthas · Level 23 Paladin".
- **Profiles:** pick which profile Continue uses. Today there's one profile, from `env/player.json`. Creating profiles from the menu is on the list.
- **Settings:** the game's own option panels, plus the login screen look: **Lich King**, **Burning Crusade** or **Classic** (the Dark Portal scene). Each switches the backdrop, logo and music together.
- **Extras:** Cinematics, Credits, and **About** (game and Moonglade versions, the commit and pins, and the current world: size and vanilla mode on/off).
- **Quit.**

**Your menu choices (last profile, backdrop) are saved only after you enter the world.** The client writes its settings file only then. If you quit from the login or character screen, nothing is saved (ADR-0009, Appendix A).

### Settings outside the game

```sh
harness/hs world-size                  # show the current setting
harness/hs world-size small            # 150 AI players
harness/hs world-size normal           # 500 (default)
harness/hs world-size large            # 1000
harness/hs world-size 300              # any number from 10 to 2000

harness/hs client-mode                 # show which client is used
harness/hs client-mode stock           # the untouched client/: classic login, type the account and password
harness/hs client-mode patched         # the play client with the Moonglade menu (default)
```

These are saved in `env/settings.json` and read on every start.

**Vanilla mode** is **on by default**. Switching it takes effect at the next server start, and it never changes a character's level, XP, items or quests. **There is no supported off-switch yet.** It's coming with the next front-menu change (world size and vanilla mode in Settings). The least-bad manual workaround: set `EraProfile.Enable = 0` in `server/etc/modules/mod_era_profile.conf.dist`. The server config is re-rendered from that file on every start, and the next `harness/hs build` overwrites it (back to on). Don't edit the repo copy under `modules/mod-era-profile/conf/`: that changes a build input and makes the `era.default_on` check fail. Don't use `HS_PROFILE=wotlk` either: it's a test hook that drops the world to 10 bots and ignores your world size.

Things to know when switching: going from vanilla to WotLK lowers the XP thresholds, so a character may level up on its next XP gain. Item tooltips can look stale until the client cache refreshes.

### Save backups

Every time you quit the game, Moonglade backs up your save while it shuts down (the shutdown window shows "Backing up your save"; about 10 s with the default world). A backup holds your characters, your companions and the AI players (the `acore_characters`, `acore_auth` and `acore_playerbots` databases) plus your client's per-account settings (`WTF/Account/<ACCOUNT>/`: key bindings, macros, chat and addon settings). Game content and vanilla mode aren't part of it. Backups live in `env/backups/` (about 38 MB each). The last 10 are kept.

**To restore one,** make sure Moonglade isn't running, then hold **Option** (⌥, "alt") while you click Moonglade in the Dock or double-click it in Finder, and keep holding until the Backups window appears. (Option-click is a stopgap; a Saves entry in the in-game front menu is planned.) Instead of starting the game it shows the Backups window: pick a backup, press **Restore…**, confirm, then **Play** or **Quit**. From a terminal: `./play --backups` lists them and `./play --backups restore <id>` restores one. A restore rewinds everything in the backup together and first saves a "before restore" safety backup of how things are now (the last 3 are kept), so you can undo it the same way. Restoring is refused while Moonglade is running, and a damaged backup is refused without changing anything. If a backup ever fails (for example, less than 2 GB free disk space), shutdown still completes, and the next start tells you why.

### Companions

Add and dismiss companions with chat commands: `.playerbots bot addclass <class>` (e.g. `priest`) and `.playerbots bot remove <name>`. These are the commands the harness checks use (`harness/hslib/companion.py`). They should work on the non-GM `PLAYER` account per the code (`.playerbots bot` is a player-level command and `AiPlayerbot.AddClassCommand` defaults to 1), but the harness doesn't exercise that (its checks use a GM-level test account), so confirm in play. Companions keep their last orders across sessions.

## Troubleshooting

- **Everything hangs, even `kill -9` doesn't work on Wine.** Rosetta's translation service (`oahd`) can wedge, which freezes every x86_64 program, Wine included (2026-10-03). Fix: `sudo killall oahd`. If that doesn't help, reboot.
- **macOS privacy prompts.**
  - *Documents folder* (Moonglade.app): allow it. If you denied it, the app says "macOS blocked access to the Moonglade folder". Fix it in System Settings > Privacy & Security > Files and Folders > Moonglade (Documents Folder), then open the app again.
  - *Screen recording*: Moonglade doesn't ask for this when you play. Only some harness checks look at window lists. A prompt seen on 2026-10-04 most likely came from another app, and denying it is safe.
  - The real Wow.exe under Wine may trigger other macOS prompts.
- **A closed lid or sleep stalls long runs.** The harness holds `caffeinate -s` during runs. The dev Mac also has `sudo pmset -a disablesleep 1` set, so a closed lid doesn't stall anything. Undo it with `sudo pmset -a disablesleep 0`.
- **"Moonglade is already running"** (app) or exit code 2 (`./play`). Another `./play` or app session holds `env/run/play.lock`, and `./play` prints its pid. Use the window that's already open, or quit that game first. A lock left by a killed `./play` is cleaned up automatically. If the server is still up with no game running, check `harness/hs status` and stop it with `harness/hs down`. If a Wine process is stuck, stop this prefix's Wine:
  ```sh
  WINEPREFIX="$PWD/env/wineprefix-mtld3d" env/wowsilicon-r17/.wine-runtime/bin/wineserver -k
  ```
  (Known nit F21: a mistyped `./play` argument also exits with 2, so the app would wrongly say "already running".)
- **The patched client misbehaves.** Go back to the untouched client with `harness/hs client-mode stock`. You'll see the classic login screen. Log in with the account and password from `env/player.json`. `harness/hs client-mode patched` switches back. To rebuild the play client, run `harness/hs client-build --force`; it keeps the play client's `WTF/` (your saved choices and game settings). Deleting `env/client-play/` also works but **resets those settings**. `client/` itself is never modified.
- **The client is stuck on "connecting".** Check `harness/hs status` and `env/logs/Auth.log`. `./play` re-points the client it launches at localhost on every start, so this is rarely the config. To fix it by hand anyway: `harness/hs client-config env/client-play` (patched mode) or `harness/hs client-config client` (stock mode).
- **The client crashes at startup.** Read the crash report in `Errors/` and the tail of `env/logs/wine.log`. Then run `harness/hs run --only client.inspect` to check for damaged client files. `.claude/skills/hs-client-debugging/SKILL.md` lists known causes.
- **Known harmless:** the worldserver can segfault *while shutting down* after a population session (F15, an upstream bug). It doesn't affect play.

**Where the logs are:**

| What | Where |
|---|---|
| App launcher | `env/logs/app.log` |
| Last `./play` run (steps, exit, cleanup) | `env/run/play-last.json` |
| Server | `env/logs/Server.log`, `Errors.log`, `worldserver.out`/`.err`, `Auth.log`, `authserver.out`/`.err` |
| Bots | `env/logs/Playerbots.log` |
| MySQL | `env/logs/mysqld.err` |
| Wine and the client's console output | `env/logs/wine.log` |
| Client logs and crash reports | `env/client-play/Logs/`, `env/client-play/Errors/` (patched mode); `client/Logs/`, `client/Errors/` (stock mode) |
| Last harness run | `harness/summary.md`, `harness/results.json` |

## Development

### The harness

`harness/hs` is the one entry point for building, running and testing. `harness/hs --help` lists every command, and `harness/hs <cmd> --help` explains each one. The main ones:

| Command | What it does |
|---|---|
| `hs bootstrap <client>` (`./bootstrap`) | the whole setup, skipping finished steps |
| `hs build [--clean \| --force-clean]` | sync the pinned sources, apply the patch queues, build and install the server |
| `hs up [--profile m0\|population\|wotlk]` / `hs down` / `hs status` | start, stop and inspect MySQL, authserver and worldserver |
| `hs run [--tier quick\|full\|slow] [--only GLOB] [--force-clean]` | run checks, then write `harness/results.json` + `harness/summary.md` |
| `hs list` | list registered checks |
| `hs gm CMD`, `hs sql DB SQL`, `hs fixture …`, `hs probe …` | drive a running server (GM over SOAP, SQL, test fixtures, the headless protocol probe) |
| `hs client-install`, `hs client-config`, `hs client-build`, `hs client-mode` | client install, localhost config, the play client, stock vs patched |
| `hs wine-install`, `hs launch [--smoke]` | the Wine runtime; start the client the harness way (test account) |
| `hs world-size`, `hs vanilla-mode`, `hs pop-session` | world size setting; whether vanilla mode is on; one measured population session |
| `hs download <name>` | one pinned, SHA-256-checked download (`client-data` is also unpacked) |
| `hs app`, `hs icon` | build `dist/Moonglade.app`; regenerate the icon |
| `hs ghidra-install`, `hs re …`, `hs mpq …` | read-only client analysis, MPQ list/extract/write (never under `client/`) |

**Tiers.** Each check is tagged quick, full or slow:
- `--tier quick` runs only the quick checks. The spec bound is under 3 min. The last run took 78 s (2026-10-05).
- `--tier full` is the default and the merge gate. It runs quick and full checks and takes about **25 min** (1526 s, 76 pass, on 2026-10-05). Most of that time goes to checks that launch the real client (login screen, play loop, app), plus a from-empty build when build inputs change (about 3.5 min with ccache).
- `--tier slow` runs everything, including bot warm-ups and the 10-minute performance soak. The last slow run (68/0, 2026-10-03, `22ef40e`) predates the M3 and login-screen checks; expect well over 30 min.

Only one agent or person owns the running stack at a time. `--leave-up` keeps it running at the end. The harness uses its own accounts (`HSTEST`, `HSCOMP`, `HS_GM`) and never touches `PLAYER` or `env/player.json`.

### How work gets done: the OpenSpec change loop

Every unit of work is an OpenSpec change in `openspec/changes/<id>/`, with a proposal, spec deltas, design and tasks. Every scenario maps to a harness check (`Check: \`id\``) or is tagged `[human-verify]` with a reason. The loop is:

1. **Propose:** `openspec new change <id>` (Node 20), then write the artifacts and run `openspec validate <id>`.
2. **Branch:** `change/<id>` from `main`, listed in `harness/active-changes` while it's being built.
3. **Build:** a builder implements one task against the spec, with a clean context.
4. **Verify:** a *fresh* verifier that didn't build the change (`.claude/agents/verifier.md`) gets only the spec deltas, the diff and the harness. It returns PASS/FAIL per scenario. Builders never grade their own work.
5. **Merge:** after a verifier PASS and a green full run, open a PR with the verdict, merge, then `openspec archive <id>` (the deltas merge into `openspec/specs/`).

The practical procedure, run budget and pre-verification checklist are in `.claude/skills/hs-change-loop/SKILL.md`. Writing checks that can actually fail is covered in `.claude/skills/hs-harness-checks/SKILL.md`. The rules themselves are in `CLAUDE.md` §4–§10. Also: **before any core or module patch, search upstream issues/PRs and newer versions first** (pin bump > cherry-pick > own patch).

### Ground rules

- **The server fork** is the pinned SHAs in `fork.lock`, plus `patches/core/`, `patches/playerbots/` and `modules/`. Core patches only when unavoidable, each with an ADR (ADR-0001, 0006, 0007, 0008).
- **`client/` is never modified.** It's the pristine base, guarded by `client.inspect` against `env/client-manifest.json`. Client patches go only into the derived play client `env/client-play/`, through the hash-checked queue `patches/client/`. Every new binary patch needs its own ADR and Zach's explicit yes (ADR-0009, CLAUDE.md §2). Reverse engineering is read-only, on copies under `env/re/` (ADR-0005).
- **Data provenance (CLAUDE.md §7).** Never type game data values from memory. Vanilla-mode values come from the pinned **VMaNGOS** 1.12 database through `tools/era-diff` (only entities present in both databases, within the 1–60 scope, override-only), and the generated SQL cites its source per row. If a value can't be sourced, it's flagged, not guessed.
- `STATUS.md` is edited only on `main`.

## Repository map

| Path | What lives there |
|---|---|
| `play` | the one-command play loop (`harness/hslib/play.py`) |
| `harness/hs`, `harness/hslib/` | the harness CLI and library: build, stack, client, Wine, play loop, app, checks (`hslib/checks/`), protocol probe (`hslib/probe/`) |
| `harness/app/launcher.m` | the Moonglade.app launcher source |
| `harness/ghidra_scripts/` | headless Ghidra scripts for `hs re` |
| `harness/active-changes` | changes currently being built or verified |
| `modules/mod-era-profile/` | our server module: vanilla mode (C++, config, generated SQL) |
| `patches/core/`, `patches/playerbots/` | patch queues applied by `hs build` |
| `patches/client/` | play-client patch records (binary and MPQ) applied by `hs client-build` |
| `client-mods/glue/` | the front-menu login screen (GlueXML Lua/XML) |
| `tools/era-diff/` | VMaNGOS vs AzerothCore diff and SQL generator |
| `tools/release/`, `.github/workflows/release.yml` | prebuilt releases: the portable server build, payload packaging (`package.sh`), `delocate.py`, the no-Homebrew smoke test, and the tag-push workflow (runs only once the repo is public) |
| `branding/` | app icon |
| `fork.lock` | every pinned upstream: core, playerbots, and each download's URL + SHA-256 (client-data, VMaNGOS, Wine runtime, Ghidra) |
| `bootstrap`, `Brewfile` | one-command setup (`harness/hslib/bootstrap.py`) and its Homebrew packages |
| `openspec/specs/`, `openspec/changes/` | capability specs; open and archived changes |
| `docs/` | ADRs, setup, research |
| `.claude/` | skills (procedures), the verifier agent, session hooks |
| `CLAUDE.md`, `STATUS.md`, `KICKOFF.md` | standing rules, live status, milestone definitions |

Generated and gitignored, never committed: `vendor/` (pinned sources), `build/`, `server/` (installed server), `data/` (`mysql/`, `client-data/`, `downloads/`, `vmangos/`), `client/` (your client), `env/` (runtime: `settings.json`, `player.json`, `client-manifest.json`, `client-play/`, `wowsilicon-r17/`, `wineprefix-*/`, `logs/`, `run/`, `re/`, Ghidra), `dist/` (the app), `baseline/` (dev-only, 2.7 GB: the unpatched baseline server built by the `core.login_logout_race_baseline` check, `harness/hslib/baseline.py`), and `harness/out/`, `harness/results.json`, `harness/summary.md`.

## Docs index

**Architecture decisions** (`docs/adr/`):

| ADR | Decision |
|---|---|
| [0001](docs/adr/0001-core-base-playerbot-fork.md) | Core base is the mod-playerbots AzerothCore fork, kept as pinned SHA + patch queue |
| [0002](docs/adr/0002-native-build-isolated-mysql.md) | Native macOS build; repo-local isolated MySQL instance |
| [0003](docs/adr/0003-server-data-from-client-data-release.md) | Server map/DBC data from the wowgaming/client-data release |
| [0004](docs/adr/0004-wine-layer-whisky.md) | Wine layer: WoWSilicon's Wine runtime + mtld3d (D3D9 to Metal), repo-local prefixes |
| [0005](docs/adr/0005-ghidra-read-only-analysis.md) | Ghidra read-only analysis of the client is allowed; patching stays gated |
| [0006](docs/adr/0006-core-patch-server-mail-guard.md) | Core patch: guard ServerMailReward's async login callback |
| [0007](docs/adr/0007-core-patch-playerbots-hook-packet-copy.md) | Core patch: give the playerbots packet hook an intact copy of the packet |
| [0008](docs/adr/0008-playerbots-patch-queue-and-group-cleanup.md) | mod-playerbots patch queue; dismissing a companion takes it out of the party |
| [0009](docs/adr/0009-client-patch-overlay.md) | Revertable client patches on a derived "play client" |
| [0010](docs/adr/0010-wine-host-shim.md) | Wine host shim for one Moonglade Dock icon (accepted, not built yet) |

**Specs** (`openspec/specs/<capability>/spec.md`): what each capability must do, with scenarios.
- `server-build`, `server-lifecycle`: build and run the server.
- `harness-control`, `harness-reporting`: drive the server; report checks and tiers.
- `client-connectivity`, `client-analysis`: client under Wine; read-only Ghidra.
- `play-launcher`, `mac-app`: `./play` and the app.
- `play-client`, `login-screen`: the patched play client and front menu.
- `world-population`, `companion-bots`, `performance-budget`: the living world and its budget.
- `era-profile`: vanilla mode.

**Other docs**
- [docs/setup.md](docs/setup.md): full setup from a fresh clone.
- [docs/client-setup.md](docs/client-setup.md): client install, localhost config, the `./play` details. Its Whisky prerequisites predate the ADR-0004 revision.
- [docs/harness-requirements.md](docs/harness-requirements.md): the original M0 harness requirements.
- [docs/gate1-era-profile-memo.md](docs/gate1-era-profile-memo.md): the Gate 1 decision on vanilla-mode scope.
- [docs/research/login-screen-spike.md](docs/research/login-screen-spike.md): how the client verifies its login screen, and the recommendation.
- [docs/research/m2-population-tuning.md](docs/research/m2-population-tuning.md): bot-count and memory measurements behind the world sizes.
- [docs/research/playerbots-scripting.md](docs/research/playerbots-scripting.md): what mod-playerbots can be driven to do in tests.
- Skills: `.claude/skills/hs-change-loop`, `hs-harness-checks`, `hs-client-debugging`, `hs-reverse-engineering`.

## Legal and scope

Moonglade is a personal, non-commercial project. It is not affiliated with or endorsed by Blizzard Entertainment. World of Warcraft and its client files belong to Blizzard Entertainment.

**Bring your own client; no game files are distributed.** This repo never contains a client, game data or a patched executable, and it doesn't point to client downloads. The client you install is your own copy. Client patches (`patches/client/`) are byte recipes keyed by the stock `Wow.exe` hash, applied locally to a derived copy; `client/` stays untouched. Stock mode works with any 3.3.5a (12340) enUS client; patched mode needs the unmodified 12340 `Wow.exe` (its SHA-256 is recorded in patch 0001). AzerothCore, mod-playerbots, the Wine runtime, Ghidra and the VMaNGOS data are fetched from their own upstreams at pinned versions (`fork.lock`) under their own licences.

**Prebuilt releases** (`tools/release/package.sh`, openspec change `release-build`) bundle the server built from those pins, Oracle MySQL and a standalone Python, each pinned in `fork.lock`. A release never contains client or game files; the Wine runtime and the server data stay first-launch downloads. Every release ships its sources alongside (Moonglade at the tag, AzerothCore and mod-playerbots at the pinned commits with our patches, the exact MySQL source) with `SHA256SUMS`, and the zip carries the bundled components' licence texts in `THIRD_PARTY/`.

## Licence

Moonglade's code is licensed under the **GNU General Public License, version 2 or (at your option) any later version** (GPL-2.0-or-later); see [LICENSE](LICENSE). That matches AzerothCore and mod-playerbots, whose files our patches change, and the VMaNGOS-derived data. [NOTICE](NOTICE) lists the parts under other terms: the OpenSpec-generated files (MIT), the VMaNGOS data attribution, and the branding (`branding/`, all rights reserved).
