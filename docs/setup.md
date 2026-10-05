# Setting up Hearthstone on a fresh Mac

These are the steps to go from a fresh clone to playing, in order. They're **manual today**. Automating them (a Brewfile, an `hs bootstrap` command) is follow-up **F6** in [STATUS.md](../STATUS.md). Until that lands, this page is the reference. Anything the repo doesn't spell out is marked **TODO: confirm**. Check those steps by hand and fix this page once you've done them.

Every command runs from the repo root.

## 0. What you need

| | |
|---|---|
| **Mac** | Apple Silicon. Developed and measured on an Apple M2 Pro with 16 GB RAM, macOS 26.6 (ADR-0002, `docs/research/m2-population-tuning.md`). 16 GB is enough for the default ~500-bot world, though memory is tight (see `openspec/specs/performance-budget/spec.md`). |
| **Disk** | About 45 GB free for everything, measured on the dev Mac (2026-10-05). The table below breaks it down. |
| **Network** | Needed only during setup, to clone the pinned sources and download data and the Wine runtime. Playing is fully offline. |
| **A WoW client** | **Not included.** You supply your own Windows **3.3.5a (build 12340) enUS** client, as a zip or a folder. |

Disk use per part (measured with `du -sh`, 2026-10-05):

| Path | Size | What |
|---|---|---|
| `client/` | 17 GB | your installed client (`hs client-install` needs 19 GB free to start) |
| `env/client-play/` | ~0 extra | the play client, an APFS copy-on-write clone of `client/`. `du` reports 17 GB, but unchanged files share storage with `client/` (ADR-0009) |
| `data/client-data/` | 3.1 GB | server map and DBC data (ADR-0003) |
| `vendor/` | 2.3 GB | pinned AzerothCore and mod-playerbots sources |
| `build/` | 1.8 GB | cmake build tree (Release) |
| `~/Library/Caches/ccache` | 1.4 GB | compiler cache, outside the repo |
| `data/mysql/` | 1.4 GB | the repo's own MySQL data (characters, world) |
| `env/ghidra_*` | 0.9 GB | optional: Ghidra, for read-only client analysis |
| `env/wowsilicon-r17/` | 0.4 GB | the Wine runtime |
| `env/wineprefix-mtld3d/` | 0.3 GB | the Wine prefix |
| `data/vmangos/` + `data/downloads/` | 0.2 GB | optional: VMaNGOS data, used only by vanilla-mode checks and the era-diff tool |
| `server/` | 52 MB | installed server binaries and configs |

## 1. Tools

```sh
xcode-select --install              # Command Line Tools: clang, xcrun, codesign (server build and Hearthstone.app)
softwareupdate --install-rosetta    # the Wine runtime is x86_64 and runs under Rosetta 2
```

Rosetta works if `arch -x86_64 /usr/bin/true` exits cleanly.

Install [Homebrew](https://brew.sh) at its default `/opt/homebrew`. The build looks for its dependencies there (`harness/hslib/build.py`, `cmake_args()`). Then:

```sh
brew install cmake mysql boost openssl@3 readline ccache python
```

These are the dev Mac's versions (`brew list --versions`, 2026-10-05): cmake 3.31.0, mysql 9.2.0, boost 1.92.0, openssl@3 3.6.x, readline 8.x, ccache 4.14.1, Python 3.14.7. The harness needs **Python 3.10 or newer** and uses only the standard library, so there's no `pip install` step. `hs app` refuses an older `python3`.

MySQL 9.x is newer than AzerothCore's documented minimum of 8.0. It works here. ADR-0002 describes a fallback in case it ever doesn't (Docker `mysql:8.4` on the same port).

**Optional:**
- `brew install openjdk@21`, for Ghidra (`hs ghidra-install`, `hs re`). This is read-only client analysis, ADR-0005.
- **Node 20** for the `openspec` CLI (`@fission-ai/openspec`, 1.14.0 on the dev Mac), needed only for development. The dev Mac uses nvm: `export PATH=~/.nvm/versions/node/v20.20.1/bin:$PATH`. Its default shell Node is 16, which is too old. Installing it: `npm install -g @fission-ai/openspec` under Node 20 (**TODO: confirm**, since the repo doesn't record how it was installed).
- `gh` (GitHub CLI), used by the development loop for PRs.

Whisky.app is **not** needed. The default Wine layer is the pinned WoWSilicon runtime from step 5. Whisky's Wine 7.7 is only an optional A/B layer (`HS_WINE=whisky`, ADR-0004). The older parts of `docs/client-setup.md` still mention Whisky as a prerequisite, and that text predates ADR-0004's revision.

## 2. Clone

```sh
git clone https://github.com/zttimmerman/[redacted].git   # private repo
cd [redacted]
```

The repo lives in `~/Documents` on the dev Mac. That's why `Hearthstone.app` asks for Documents-folder access on its first launch (see the README's Troubleshooting section). Another location works too. **TODO: confirm** whether the macOS prompt changes outside `~/Documents`.

## 3. Build the server

```sh
harness/hs build
```

This clones the pinned sources into `vendor/` (core `mod-playerbots/azerothcore-wotlk@Playerbot` and `mod-playerbots/mod-playerbots`, the SHAs in `fork.lock`). It applies the patch queues `patches/core/*.patch` and `patches/playerbots/*.patch` in order, links `modules/*` (our `mod-era-profile`), then runs cmake configure, build and install into `server/`. It uses 8 parallel jobs by default (`-j N` changes that). A patch that no longer applies stops the build loudly (ADR-0001).

- `harness/hs build --clean` does a from-empty build only when the build inputs changed (pins, patches, modules, cmake args, compiler). Otherwise it proves the install is current.
- `harness/hs build --force-clean` always wipes `build/` first.
- A rebuild with a warm ccache took about 3.5–5 min (harness-tiers proposal, 2026-10-02). **TODO: confirm** how long a cold first build takes.

## 4. Server data (client-data)

The worldserver needs DBC, map, vmap and mmap files. These come from the pinned `wowgaming/client-data` release (ADR-0003), not from your client. **No `hs` command downloads them yet** (part of F6). Get them by hand:

```sh
mkdir -p data/downloads data/client-data
curl -L -o data/downloads/Data.zip "$(grep '^client_data_url=' fork.lock | cut -d= -f2-)"
unzip -q data/downloads/Data.zip -d data/client-data
ls data/client-data   # expect: Cameras  dbc  maps  mmaps  vmaps
```

**TODO: confirm** that the zip unpacks straight into those five folders. If it has a top-level folder, move its contents up. The server is configured with `DataDir = data/client-data` (`harness/hslib/config.py`), and the harness reads `data/client-data/dbc/`.

**Optional, for development only:** the vanilla-mode checks and `tools/era-diff` read the pinned VMaNGOS database. Download it to the path the tool expects, and the tool unzips it into `data/vmangos/` by itself:

```sh
curl -L -o data/downloads/vmangos-db-sqlite.zip "$(grep '^vmangos_db_url=' fork.lock | cut -d= -f2-)"
```

## 5. MySQL and the first server start

You don't install a database by hand. The harness runs its **own** `mysqld` (Homebrew's binary) with its data in `data/mysql/`, on `127.0.0.1:3307`. It never touches Homebrew's MySQL service or its data (ADR-0002). The first start initializes it:

```sh
harness/hs up       # mysqld, authserver, worldserver; waits until they're ready
harness/hs status   # processes, ports, readiness
harness/hs down     # clean shutdown
```

On the first start, AzerothCore creates and fills its databases (`acore_auth`, `acore_world`, `acore_characters`, `acore_playerbots`). That's slow, so the harness allows up to 30 minutes for it (`stack.py`). Later starts take seconds to a few minutes.

Everything listens on localhost only: MySQL 3307, auth 3724, world 8085, SOAP 7878. Server configs are regenerated from `server/etc/*.conf.dist` on every start, so never hand-edit `server/etc/*.conf`.

This step is optional. `./play` starts the stack by itself, but running it once here surfaces build or data problems early.

## 6. Wine runtime

```sh
harness/hs wine-install
```

This downloads the pinned WoWSilicon Wine runtime r17 (Wine 11.13 with mtld3d, which turns D3D9 into Metal), checks its SHA-256 against `fork.lock`, and unpacks it into `env/wowsilicon-r17/`. It never modifies anything on the client (ADR-0004). It's idempotent. `--force` downloads it again. The Wine prefix `env/wineprefix-mtld3d/` is created automatically on the first launch.

## 7. Install and check your client

```sh
harness/hs client-install ~/Downloads/YourClient_3.3.5a.zip   # or a folder
```

This reads the source **read-only** and installs it into `client/`. It checks every zip member's CRC, then writes `env/client-manifest.json` (hashes of every `.exe`/`.dll`, the MPQ list, file sizes, and the Wow.exe version, which should be `3.3.5.12340`). Finally it points the client at `127.0.0.1` (`realmlist.wtf`, `WTF/Config.wtf`). It's safe to re-run. `docs/client-setup.md` has the details, including what to do when the source zip is damaged.

Check the install at any time:

```sh
harness/hs run --only client.inspect
```

`client/` is the pristine base and is **never modified**. Patches go into the play client (next step).

## 8. Build the play client

```sh
harness/hs client-build
```

This builds `env/client-play/`: an APFS clone of `client/`, plus the hash-checked patch queue in `patches/client/`. That queue holds `0001` (an 8-byte change so the client accepts a modified login screen, ADR-0009 Appendix A) and `0002` (the Hearthstone front menu, built from `client-mods/glue/`). It refuses a client that doesn't match the expected hashes. `./play` also runs this automatically when needed, so this step mostly confirms that it works. The build keeps your settings in the play client's `WTF/`, `Screenshots/` and so on across rebuilds.

## 9. Build the app

```sh
harness/hs app
```

This builds `dist/Hearthstone.app`: a small launcher compiled from `harness/app/launcher.m`, ad-hoc signed, with the icon from `branding/AppIcon.icns`. It records this repo's path, so you can drag the app to `/Applications` or the Dock and it still finds the repo. It writes nothing outside the repo. (`harness/hs icon` regenerates the icon from `env/branding/logo-source.jpeg`, a file that isn't committed. You only need it if you change the logo.)

## 10. First run

Double-click `dist/Hearthstone.app`, or run `./play` in a terminal.

- The first run creates your personal account, **`PLAYER`**, with a random password. It's not a GM account. The name and password are saved in **`env/player.json`** (gitignored, readable only by you). `./play` prints them once. To choose your own name and password instead, create that file *before* the first run: `{"account": "NAME", "password": "secret"}` (letters and digits, up to 16 characters each).
- With the default (`patched`) client, the login screen shows the Hearthstone menu. Press **Continue** (or Enter) and you're in, with nothing to type. Create a character at character select as usual.
- Quitting the game shuts everything down. `./play` prints `All stopped.`

## 11. Optional: run the harness

```sh
harness/hs run --tier quick   # about 1–2 min
```

See the README's Development section for the tiers. The harness uses its own accounts (`HSTEST`, `HSCOMP`, `HS_GM`) and never touches `PLAYER`.

## Optional: Ghidra (read-only client analysis)

```sh
brew install openjdk@21
harness/hs ghidra-install
```

This unpacks the pinned Ghidra into `env/` (ADR-0005). See `.claude/skills/hs-reverse-engineering/` for how `harness/hs re` is used.
