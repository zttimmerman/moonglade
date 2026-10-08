## Context
`release-build` (1/3) produced `env/release/payload/Moonglade/`: a relocatable server, Oracle MySQL 8.4.11, python-build-standalone 3.13, the harness, the pruned SQL tree and the prebuilt launcher, proven to run without Homebrew from two locations. Its "As built" leaves three duties here: module keys as `AC_*` env (the server's `CONF_DIR` is the relative `etc`), and setting `SourceDirectory` and `MySQLExecutable` (allowlisted compiled-in defaults name the build machine) plus `OPENSSL_CONF`.

The harness today assumes a checkout: `paths.ROOT` is the repo, runtime state lives in `env/` and `data/`, `stack.MYSQLD` is Homebrew's, `config.common` points `SourceDirectory` at `vendor/` and `MySQLExecutable` at Homebrew, and the launcher reads an absolute `repo-path` resource and runs `<repo>/play` with a `python3` from PATH.

## Goals / Non-goals
- Goals: a friend double-clicks `Moonglade.app`, gives their client folder once, and plays; the dev checkout behaves exactly as today.
- Non-goals: signing, notarisation, the icon tile, the Info.plist line (`release-signing`); a prebuilt database template (measure only); updating an installed release to a newer one (later; HOME's layout carries a version so it can be added); an uninstall button; a choice of HOME location.

## Decisions

### 1. Mode and paths: one resolver
- `paths.py` gains `MODE` (`dev` | `release`), `APP` and `HOME`, decided once at import. Release iff the payload root (`ROOT`, already computed from `__file__`) has `release-build`'s `VERSION` JSON and no `.git`. A git checkout is always dev, so `./play`, `hs` and the dev app keep today's paths byte for byte (`release.mode_resolution` compares the full exported table against one recorded from the parent commit).
- Dev: `HOME` = `ROOT` and every existing name keeps its value. Release: `HOME` = `~/Library/Application Support/Moonglade`, laid out as:

  | HOME path | holds | dev equivalent |
  |---|---|---|
  | `run/`, `logs/`, `tmp/` | pid/lock files, logs (incl. `app.log`, `first-launch.log`) | `env/run`, `env/logs`, `env/tmp` |
  | `etc/` | rendered `authserver.conf`, `worldserver.conf`, `ac-env.json` | `server/etc` |
  | `mysql/`, `my.cnf` | the datadir | `data/mysql`, `env/my.cnf` |
  | `client-data/` | the pinned server data | `data/client-data` |
  | `downloads/` | download cache (`.part` while downloading; zips removed after unpack) | `data/downloads` |
  | `wine/runtime/`, `wine/prefix/` | the pinned Wine runtime and its prefix | `env/wowsilicon-r17`, `env/wineprefix-mtld3d` |
  | `client/` + `client-manifest.json` | Moonglade's copy of the friend's client (pristine; never patched) | `client/`, `env/client-manifest.json` |
  | `client-play/` | the derived play client (ADR-0009) | `env/client-play` |
  | `settings.json`, `player.json`, `backups/` | the player's files | `env/…` |
  | `home.json` | layout version + the first-launch step record | — |
- Every module that builds a path from `P.ROOT`, `P.ENV`, `P.DATA` or `P.BREW` takes it from the resolver instead. A static check inside `release.mode_resolution` greps `hslib/` for new `P.ROOT /`/`P.ENV /`/`P.BREW` uses outside `paths.py` and a reviewed list (dev-only tools: build, baseline, ghidra, release packaging), so a later change can't reintroduce a repo path into release mode.
- Lanes stay dev-only: release mode refuses `HS_LANE=b`.
- Test switches (refused unless `--allow-test-release` and a live harness run, like `HS_BOOTSTRAP_TEST`): `HS_RELEASE_HOME` (scratch HOME; never the real one, never inside `env/` except `harness/out/release-scratch/`), `HS_RELEASE_PORTS` (scratch ports), `HS_RELEASE_DOWNLOAD_BASE` (loopback URL base for pinned downloads; hashes still from `fork.lock`), `HS_FIRSTLAUNCH_TEST` (injected Mac facts, free bytes, the corrupt-copy and drop-`AC_*` counterexamples). The launcher adds `HS_APP_TEST_CLIENT_PICK` (scripted folder pick), honoured only with its existing test hook, and strips all of them from the play loop's environment as it does today.

### 2. Release app shape
- The zip's top level becomes `Moonglade.app`: `Contents/MacOS/Moonglade` (the launcher), `Contents/Info.plist`, `Contents/Resources/AppIcon.icns`, and the unchanged `release-build` payload at `Contents/Resources/Moonglade/`. The launcher finds the payload from `[NSBundle mainBundle]` (no absolute `repo-path`), so moves and translocation just work. The dev app keeps `repo-path`.
- Bundle identifier for now `local.moonglade.release`, so Zach's dev app (`local.moonglade.app`) and a release copy on the same Mac don't share privacy grants. `release-signing` picks the final one.
- Mach-O files under `Contents/Resources` are fine for ad-hoc signing; whether notarisation wants them under `Contents/Helpers`/`Frameworks` is `release-signing`'s call. Only `paths.APP` and `release.py`'s staging would move.
- `release-build`'s checks follow the new payload root (task 1.3). Their assertions don't change.

### 3. Release stack
- `stack` takes `MYSQLD`, `MYSQLADMIN` and `MYSQL` from the resolver (release: `APP/mysql/bin/…`; dev: Homebrew as today). `my.cnf` in release adds `basedir`, `lc-messages-dir` and `character-sets-dir` pointing into `APP/mysql/share`.
- `config.common` in release: `SourceDirectory` = `APP/sql` (its `data/sql/…` and `modules/<m>/data/sql` layout is the updater's shape; task 2.2 verifies this against `DBUpdater.cpp` and fixes the packaging if not), `MySQLExecutable` = `APP/mysql/bin/mysql`, `DataDir` = `HOME/client-data`, logs/tmp/pid in HOME.
- Module keys: release reuses lane B's mechanism (`config.module_env`, `stack.spawn_env`). Confs render to `HOME/etc`; the module values go to `HOME/etc/ac-env.json` and into the server's environment. Servers run with `cwd` = `HOME/run`, so the relative compiled-in `etc/modules` resolves to a folder that doesn't exist and no stale module conf can be read.
- `OPENSSL_CONF` = an empty `HOME/etc/openssl.cnf` written at render time, so OpenSSL never opens the allowlisted build-machine `OPENSSLDIR`.
- Ports: release uses lane A's numbers (3307/3724/8085/7878; the client's realmlist uses 3724). So Zach can't run his dev stack and a release copy at the same time; first launch and the play loop already refuse when "something of Moonglade is running", which covers it. Checks use `HS_RELEASE_PORTS`.
- Python: the launcher runs `APP/python/bin/python3 -I -m hslib.play` (the payload's `.pth` puts `harness/` on the path); `-I` keeps PYTHONPATH and user site-packages out.

### 4. First launch
- **Engine:** a release step list in `bootstrap.py` reusing its framework (honest `done()`, `[bootstrap] {json}` events, stop at first failure with a hint): `preflight → client → data → wine → database → playclient → defaults`. No brew, build or app steps. `hs first-launch` runs it; the launcher runs it as a child and parses the events the way it parses `[stage]` lines.
- **Window:** the loading window's look (title, label, determinate bar, one detail line), with three extra states: "Choose your WoW 3.3.5a folder" (button → `NSOpenPanel`, folders and zips), an error state (plain message + "Choose again"/"Quit"), and "All set" (text + Play). Closing the window mid-setup asks the child to stop (SIGTERM) and quits; the next open continues.
- **Client step:** `check_client_source` (existing: `Wow.exe` version 3.3.5.12340, enUS) then `clientinst.install(src, HOME/client, HOME/client-manifest.json)`, which only reads the source and verifies the copy. When source and HOME share an APFS volume, files are cloned (`clonefile`), so the copy is instant and takes no extra space until the friend deletes their folder; otherwise a byte copy. The "you can delete the folder" line is shown only after the manifest check passes.
- **Never writing the friend's folder:** the existing pristine rules (`pristine.py`) are extended to "the source given to first launch": the client step refuses a destination inside the source and vice versa, and checks run it under a sandbox profile denying writes to the source.
- **Downloads:** `downloads._download` gains a progress callback (bytes, total from `Content-Length`) emitted as events at most every 250 ms. On failure, `.part` is removed and the step stays not done; resume is per step, not per byte (server data is 1.2 GB; byte-range resume is a later nicety). `HS_RELEASE_DOWNLOAD_BASE` swaps only the URL host for tests; the sha256 always comes from `fork.lock`.
- **Database step:** payload `mysqld --initialize-insecure` into `HOME/mysql`, then the existing first start (`stack.up` m0 + `down`) with the release config. Before initialising it records "created by first launch, attempt N" in `home.json`; a later attempt may move an unfinished datadir aside (never delete it) only if that record exists and `acore_characters.characters` is empty; anything else stops with a message. The step logs timestamps for: mysqld initialise, SQL import (updater start → last "applied" line per database), bot setup (worldserver world-ready → playerbots ready), so task 4.1 can record the split.
- **Disk check:** need = size of the given client (from its listing) + the fixed part, where fixed = server data unpacked (3.1 GB) + its zip while unpacking (1.2 GB) + databases (1.4 GB) + Wine runtime and prefix (0.7 GB) + 2 GB headroom ≈ 8.4 GB, so ≈ 26 GB with an 18 GB client (sizes from `docs/setup.md` "Disk", measured 2026-10-07 on the dev Mac). Task 4.1 re-measures on a scratch HOME and replaces these numbers with the measured ones, cited here. With an APFS clone on the same volume the real use is lower; the check still asks for the full amount (the friend will delete their copy).
- **Rosetta:** never installed by Moonglade. The message says to run `softwareupdate --install-rosetta --agree-to-license` in Terminal (or open any Intel app, which makes macOS offer it), then open Moonglade again.

### 5. Plain-language errors (first-launch catalog)
Each failure maps to one message with what happened, whether anything changed, and what to do: not Apple Silicon; macOS too old (needed vs found); no Rosetta; Moonglade already running; not enough space (needed vs free, which disk); not a WoW folder; wrong version (found version, needed 3.3.5a build 12340); not English; can't read the folder (privacy prompt declined: how to allow it in System Settings); download failed (check internet, open again); download damaged; database setup failed (log path); unexpected error (log path). `firstlaunch.*` checks assert the key phrase of each.

## Risks / Trade-offs
- **Folder privacy prompts:** a non-sandboxed app reading `~/Downloads`/`~/Documents` triggers TCC once; declining makes the client step fail with the "can't read" message. Not automatable without changing TCC; human-verify.
- **Disk:** a full byte copy doubles the client until the friend deletes theirs; the up-front check prevents running out mid-way.
- **Same ports as dev:** Zach can't run both at once; acceptable, and the running-check says so plainly.
- **18 GB copies in checks:** checks clone from `client/` on the same APFS volume, so they cost seconds and ~0 disk; `firstlaunch.kill_resume` forces the byte-copy path for its copy-phase kill (test switch) so the slow path is covered too, on a scratch subset fixture rather than 18 GB.

## Checks (all `stack="down"` or scratch ports; one shared first-launch fixture per run, like `release-build`'s shared packaging)
- `release.mode_resolution` (quick), `release.test_switches_refused` (quick), `release.stack_from_payload` (slow), `release.app_readonly` (slow), `release.app_moved` (slow), `release.play_standin` (slow), `release.client_smoke` (slow, GUI), `release.player_data_untouched` (full; order 998, after every release check, like `client.player_client_not_rebuilt`).
- `firstlaunch.flow` (slow, GUI via test hook), `firstlaunch.second_open_plays` (slow, GUI), `firstlaunch.source_untouched` (slow), `firstlaunch.client_refusals` (full), `firstlaunch.preflight` (quick), `firstlaunch.downloads` (full), `firstlaunch.kill_resume` (slow).
- Every release-mode check runs the payload under `release-build`'s sandbox profile (deny reads of `/opt/homebrew`, `/usr/local`; deny writes outside scratch), extended to deny reads of the dev repo's `vendor/`, `server/`, `build/`, `data/` and `env/` (except `env/release/` and the scratch dirs).

## Measurements
(Builder fills in, task 4.1: database first start split — mysqld initialise / SQL import / bot setup — on a scratch HOME, ×3, cold disk cache noted; first-launch total time by step; HOME size by folder after setup, with and without the APFS clone; the fixed-part disk number for preflight.)

## As built
(Builder fills in.)
