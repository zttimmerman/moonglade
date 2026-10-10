# Setting up Moonglade on a Mac

One command takes a fresh clone to a ready `dist/Moonglade.app`:

```sh
./bootstrap ~/Downloads/YourClient_3.3.5a.zip     # or the folder that holds Wow.exe
```

This page is the detailed reference: what you need, what each bootstrap step does, how long it takes, and how to do any step by hand. Every command runs from the repo root.

## 0. What you need

| | |
|---|---|
| **Mac** | Apple Silicon (M1 or newer). **macOS 26.2 or newer**: the pinned Wine runtime's binaries are built for macOS 26.2 (their `LC_BUILD_VERSION` minimum, read with `otool -l`), and macOS refuses to load a binary built for a newer system. Developed and measured on an M2 Pro with 16 GB RAM, macOS 26.6 (ADR-0002, `docs/research/m2-population-tuning.md`). 16 GB is enough for the default ~500-bot world, though memory is tight (`openspec/specs/performance-budget/spec.md`). |
| **Disk** | About **28 GB** for a full install, measured by a fresh bootstrap on the dev Mac (2026-10-07; table below), plus headroom. Bootstrap checks free space first and needs about 35 GB free for a first run (the client install alone wants 19 GB free when it starts). |
| **Network** | Only during setup: the pinned sources (about 1.4 GB of git history), the server data (1.2 GB) and the Wine runtime (64 MB). Playing is fully offline. |
| **A WoW client** | **Not included.** You supply your own Windows **3.3.5a (build 12340) enUS** client, as a zip or a folder. Bootstrap reads it and never changes it. |
| **Tools** | Xcode Command Line Tools, Rosetta 2 and Homebrew (step 1). Bootstrap checks for each and tells you the exact command when one is missing. |

Disk use per part (fresh bootstrap on the dev Mac, 2026-10-07; `du`):

| Path | Size | What |
|---|---|---|
| `client/` | 17.9 GB | your installed client (a copy; `hs client-install` needs 19 GB free to start) |
| `env/client-play/` | ~0 extra | the play client, an APFS copy-on-write clone of `client/`. `du` reports 17 GB, but unchanged files share storage with `client/` (ADR-0009) |
| `data/client-data/` | 3.1 GB | server map and game-table data (ADR-0003) |
| `vendor/` | 2.3 GB | pinned AzerothCore and mod-playerbots sources |
| `build/` | 1.8 GB | cmake build tree (Release) |
| `data/mysql/` | 1.4 GB | the repo's own MySQL data (characters, world) |
| `env/wowsilicon-r17/` + `env/wineprefix-mtld3d/` | 0.7 GB | the Wine runtime and its prefix |
| `server/` | 52 MB | installed server binaries and configs |
| `~/Library/Caches/ccache` | about 1.5 GB | compiler cache, outside the repo (Homebrew's ccache default) |
| `baseline/` | 2.7 GB | dev-only: the unpatched baseline server built by the `core.login_logout_race_baseline` check |
| `env/ghidra_*` | 0.9 GB | optional: Ghidra, for read-only client analysis |

## Moonglade's defaults

After setup you play with the same settings as the dev Mac. This table is generated from `harness/hslib/settings.py` (`DEFAULTS`, the one place they're defined; bootstrap writes them, and the `setup.fresh_clone` check compares this table with that source):

<!-- defaults:begin -->
| Setting | Default | What it means | Show it | Change it |
|---|---|---|---|---|
| World size | **normal** | about 500 AI players | `harness/hs world-size` | `harness/hs world-size <small, normal, large or a number>` |
| Vanilla mode | **on** | levels 1-60 at the pre-2.3 pace and difficulty | `harness/hs vanilla-mode` | no supported switch yet (see the README) |
| Game client | **patched** | the play client with the Moonglade front menu | `harness/hs client-mode` | `harness/hs client-mode <stock or patched>` |
<!-- defaults:end -->

Bootstrap only fills in a setting that isn't set yet; a choice you already made in `env/settings.json` is kept.

## 1. Before bootstrap: the tools

```sh
xcode-select --install                                  # Command Line Tools: clang, git, codesign
softwareupdate --install-rosetta --agree-to-license     # the Wine runtime is x86_64 and runs under Rosetta 2
```

Install [Homebrew](https://brew.sh) at its default `/opt/homebrew` (the build looks for its packages there):

```sh
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
eval "$(/opt/homebrew/bin/brew shellenv)"   # put Homebrew on PATH; add this line to ~/.zprofile to keep it
```

If `python3 --version` says less than 3.10 (a new Mac's is 3.9), `./bootstrap` stops and says `brew install python`. Everything else it installs from the `Brewfile` itself, with your OK.

**A second macOS account on a Mac that already has Homebrew** (e.g. a friend's account on your Mac, or a test account): `/opt/homebrew` belongs to the account that installed it, but everything in it is readable and runnable by every account (`drwxr-xr-x`). Moonglade only reads and runs Homebrew's tools (cmake, clang via ccache, mysqld with the repo's own data folder), so a second account can use them as they are. It can't *install* packages there: if `./bootstrap` reports a missing package, the account that owns `/opt/homebrew` has to run the `brew bundle install` line it prints.

## 2. Clone and bootstrap

```sh
git clone https://github.com/zttimmerman/moonglade.git
cd moonglade
./bootstrap ~/Downloads/YourClient_3.3.5a.zip
```

Any folder works: nothing depends on where the repo lives. (If the repo is in `~/Documents`, macOS asks once whether Moonglade may use the Documents folder; allow it.)

Options: `--yes` installs missing Homebrew packages without asking; `--check` only checks the Mac and the client and changes nothing. Once `client/` is installed, a later `./bootstrap` doesn't need the client argument.

**Every step is skipped when it's already done**, judged by its output (not by a "done" note): running `./bootstrap` again takes seconds and changes nothing. If a run is interrupted (Ctrl-C, a crash, a closed lid), run it again and it continues at the step that was running. When a step fails, bootstrap stops and prints the step, the problem and what to do.

The steps, in order, with the times measured on the dev Mac (2026-10-07):

| # | Step | What it does | By hand |
|---|---|---|---|
| 1 | Checking this Mac and your client | Apple Silicon, macOS 26.2+, Rosetta, Command Line Tools, Homebrew at `/opt/homebrew`; the client is a zip or folder holding a 3.3.5a build 12340 enUS Wow.exe (read straight from the zip, nothing unpacked); nothing of Moonglade running; enough free disk for the steps still to do. Nothing changes before this passes. | `./bootstrap --check <client>` |
| 2 | Homebrew packages | `brew bundle check --no-upgrade --file=Brewfile`; when something is missing, it shows the list and asks (or `--yes`), then `brew bundle install --no-upgrade`. It never upgrades or removes what you have. | `brew bundle install --no-upgrade --file=Brewfile` |
| 3 | Building the game server | clones the pinned sources into `vendor/`, applies the patch queues, builds and installs into `server/` (about 15-30 min the first time; minutes with a warm compiler cache) | `harness/hs build --clean` |
| 4 | Server data | downloads the pinned server data (1.2 GB), checks its SHA-256 against `fork.lock`, unpacks it into `data/client-data/` | `harness/hs download client-data` |
| 5 | Creating the game databases | the first server start: the repo's own MySQL on 127.0.0.1:3307 (never Homebrew's MySQL service) creates and fills the game databases, then everything stops again | `harness/hs up && harness/hs down` |
| 6 | Wine runtime | downloads the pinned WoWSilicon Wine runtime r17, checks its SHA-256, unpacks it into `env/wowsilicon-r17/` | `harness/hs wine-install` |
| 7 | Installing your game client | copies your client into `client/` (CRC-checked; your zip or folder is only read), points it at this Mac, then runs the `client.inspect` check | `harness/hs client-install <client>` |
| 8 | Building the play client | `env/client-play/`: a copy-on-write clone of `client/` plus the hash-checked patch queue `patches/client/` (the Moonglade front menu, ADR-0009) | `harness/hs client-build` |
| 9 | Building Moonglade.app | `dist/Moonglade.app`, ad-hoc signed | `harness/hs app` |
| 10 | Moonglade's default settings | the defaults above into `env/settings.json`, where unset | `harness/hs world-size normal`, `harness/hs client-mode patched` |

Measured on the dev Mac (M2 Pro, 2026-10-07): a fresh bootstrap took **36 minutes** with an empty compiler cache (32 of them cloning and building the server) and **7 minutes** with a warm one (server 5 min, databases 1.5 min, everything else under a minute; the server data and Wine runtime came from a download cache). A re-run took **2 seconds**. See `openspec/changes/easy-setup/design.md` ("Measurements") for the numbers and how they were taken.

### Pinned downloads

Every file setup downloads is pinned in `fork.lock` by URL **and SHA-256** (server data, VMaNGOS data, Wine runtime, Ghidra) and checked before use. A file with the wrong hash is refused and removed, and nothing is unpacked from it; run the command again to download a fresh copy.

To keep the downloads for a later setup (another clone, a reinstall), point `HS_DOWNLOAD_CACHE` at a folder: bootstrap looks there first (checking each file's hash like a fresh download) and keeps what it downloads there.

### If you move or rename the repo folder

The server build has the repo's folder built in. After a move, `./play` and `harness/hs up` stop and say so, naming the old and the new folder; rebuild once with `harness/hs build --force-clean` (about 15 minutes with a warm compiler cache). `harness/hs build` notices it by itself and rebuilds from empty, and the dev-only baseline build discards its old build tree by itself.

## 3. First run

Double-click `dist/Moonglade.app` (or drag it to the Dock), or run `./play` in a terminal (that needs Homebrew's Python on your PATH: the `brew shellenv` line from step 1).

- The first run creates your personal account, **`PLAYER`**, with a random password. It's not a GM account. The name and password are saved in **`env/player.json`** (gitignored, readable only by you). To choose your own name and password instead, create that file *before* the first run: `{"account": "NAME", "password": "secret"}` (letters and digits, up to 16 characters each).
- The login screen shows the Moonglade menu. Press **Continue** (or Enter) and you're in. Create a character at character select as usual.
- **Esc pauses the world.** Opening the game menu stops everything: monsters, your companions, buffs, DoTs, cooldowns and respawns. The screen dims and shows "Paused"; closing the menu picks up exactly where it stopped. Buff timers and cooldown spirals the game draws itself may jump back by the paused time when you resume. You can't pause on a flight path, boat or zeppelin (the game says so), a spell you were casting is cancelled, and Exit to Menu / Exit Game resume first. There's no player switch to turn pausing off yet; for testing, `harness/hs up --profile pause-off` starts the server with `Moonglade.Pause.Enable = 0`.
- Quitting the game shuts everything down. `./play` prints `All stopped.`

## Using a release

A release is a ready-made `Moonglade.app`: no Terminal, Homebrew or Xcode tools. You still bring your own Windows **3.3.5a (build 12340) enUS** game, as a folder or a zip.

### Opening it the first time

Download the zip, unzip it (double-click) and move `Moonglade.app` to Applications. No game files are included: you bring your own game. What happens when you open it depends on how the release was signed; its release notes say which.

- **Most releases (not registered with Apple):** macOS can't check apps that aren't registered with Apple, so it refuses the first open and says it can't verify Moonglade. Click Done. Then go to System Settings → Privacy & Security, scroll down and click **Open Anyway**, then confirm (your Mac may ask for your password). Moonglade opens, and from then on it opens normally. You do this once for each new version: macOS treats every new download as new. Prefer Terminal? After moving Moonglade to Applications, this line does the same as Open Anyway: `xattr -dr com.apple.quarantine /Applications/Moonglade.app` (it removes the "downloaded from the internet" mark from Moonglade only).
- **Releases registered with Apple (notarized):** macOS asks once whether to open an app downloaded from the internet; click Open.

What an unregistered release can't do, in plain words: Apple hasn't scanned it, so macOS can't vouch for it, and you approve each new version yourself (above). Moonglade asks for one permission at most, to read the game folder you pick (often in Downloads or Documents); macOS remembers that answer across new versions.

### What the first open does

**The first time you open it**, Moonglade checks this Mac (Apple Silicon, macOS 26.2 or newer, Rosetta) and the free space, then asks for your game folder. It only reads that folder, never changes it, and copies what it needs. Then it downloads the world's maps (1.2 GB) and the part that runs the Windows game (64 MB), creates your game world, and prepares the game. On the dev Mac this took two minutes in all (most of it creating the game world); a slower disk or connection takes longer. If you close the window or something goes wrong, the next open continues where it stopped. When it says it's done, you can delete the folder you gave it: Moonglade keeps its own copy. Later opens go straight to the game.

**Disk:** setup needs free space for your game (about 18 GB) plus about 8.6 GB for everything else, so about 27 GB free with an 18 GB game. If your game folder is on the same disk, Moonglade's copy shares its storage with it until you delete your folder, so the copy itself takes almost no extra space at first.

**What goes where.** The app holds only Moonglade itself and is never written to, so you can keep it anywhere and move it later. Everything Moonglade makes lives in one folder, `~/Library/Application Support/Moonglade` (in Finder: Go > Go to Folder…, then paste that path):

| In that folder | What |
|---|---|
| `client/` | Moonglade's copy of your game (write-protected, like the folder you gave it) |
| `client-play/` | the game Moonglade starts (built from `client/`; shares its storage) |
| `client-data/` | the world's maps the game world reads |
| `mysql/` | your game world: characters, the AI players, everything you've done |
| `backups/` | automatic save backups (the last 10) |
| `wine/` | the part that runs the Windows game on this Mac |
| `player.json` | your login (account `PLAYER` and its password) |
| `settings.json` | Moonglade's settings |
| `logs/`, `run/`, `tmp/`, `etc/`, `downloads/`, `home.json` | logs and Moonglade's own bookkeeping |
| `mysql.unfinished-<date>` (only after an interrupted setup) | a game world from a setup that was stopped before it finished, set aside instead of deleted. Moonglade keeps only the newest one; it removes an older one only when it holds no characters of yours (the AI players' own don't count), and never one that does. You can delete it yourself once setup has finished. |

**Removing everything.** Quit Moonglade first. Drag `Moonglade.app` to the Trash. Then delete the `Moonglade` folder in `~/Library/Application Support`. This deletes your characters and save backups too. Finder asks you to confirm because Moonglade's copy of your game is locked (so nothing changes it by accident); confirm and empty the Trash. Or, in Terminal, this one line unlocks the folder and removes it:

```
chflags -R nouchg ~/Library/Application\ Support/Moonglade && chmod -R u+w ~/Library/Application\ Support/Moonglade && rm -rf ~/Library/Application\ Support/Moonglade
```

Measured on the dev Mac (2026-10-08, three first launches on a scratch copy of this folder): `client/` and `client-play/` 17.9 GB each but shared with your game folder until you delete it, `client-data/` 3.3 GB, `mysql/` 1.2 GB, `wine/` 0.8 GB, each save backup about 27 MB. With your game folder on the same disk, setup used about 5 GB of free space. Details: `openspec/changes/release-first-launch/design.md`, "Measurements".

## 4. Optional

**Run the harness:** `harness/hs run --tier quick` (about 2 min). See the README's Development section for the tiers. The harness uses its own accounts (`HSTEST`, `HSCOMP`, `HS_GM`) and never touches `PLAYER`.

**VMaNGOS data** (development only; the vanilla-mode checks and `tools/era-diff` read it): `harness/hs download vmangos` puts the pinned, SHA-256-checked zip in `data/downloads/`, and `tools/era-diff` unpacks it into `data/vmangos/` after checking the hash again.

**Ghidra** (read-only client analysis, ADR-0005): `brew install openjdk@21`, then `harness/hs ghidra-install`.

**Development tools:** Node 20 for the `openspec` CLI (`npm install -g @fission-ai/openspec`, run under Node 20; e.g. `nvm use 20`), and `gh` for pull requests.

**Versions on the dev Mac** (`brew list --versions`, 2026-10-05): cmake 3.31.0, mysql 9.2.0, boost 1.92.0, openssl@3 3.6.x, readline 8.x, ccache 4.14.1, Python 3.14.7. MySQL 9.x is newer than AzerothCore's documented minimum of 8.0 and works here; ADR-0002 describes a fallback (Docker `mysql:8.4` on the same port). The harness needs Python 3.10 or newer and uses only the standard library (no `pip install`).

Whisky.app is **not** needed. The default Wine layer is the pinned WoWSilicon runtime from step 6. Whisky's Wine 7.7 is only an optional A/B layer (`HS_WINE=whisky`, ADR-0004).
