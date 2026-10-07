# Design: Easy setup

## Shape
- `./bootstrap` (repo root, POSIX sh): check Python ≥3.10 is available. If it isn't (a fresh Mac), print the Homebrew line
  to install it and stop. Then exec `harness/hs bootstrap "$@"`.
- `harness/hslib/bootstrap.py`: an ordered list of steps, each with `done()` (a cheap, honest check that the output exists and
  is current, never a self-written "done" marker alone), `run()`, a plain-language label, and a recovery hint on failure. Steps:
  1. preflight: arm64, macOS ≥ the documented minimum, Rosetta (`arch -x86_64 /usr/bin/true`), Xcode CLT
     (`xcode-select -p`), Homebrew at /opt/homebrew, free disk vs the documented need;
  2. Brewfile (`brew bundle check`, then `brew bundle` with consent);
  3. vendor sync + `hs build --clean`;
  4. server data download (pinned, sha256-verified) + unpack;
  5. DB init + first `hs up`/`hs down`;
  6. `hs wine-install`;
  7. `hs client-install <client>` + `client.inspect`;
  8. `hs client-build`;
  9. `hs app`;
  10. the defaults written via the existing settings code.

  Reuse the existing `hs` subcommands; don't duplicate their logic.
- Download cache: `HS_DOWNLOAD_CACHE` (a folder of pinned files, each verified by sha256) lets checks and repeat setups
  avoid re-downloading. A test switch, refused outside the harness unless it is just a cache folder, which is safe
  because every file is verified anyway.
- `setup.fresh_clone` is a **slow-tier** check (it builds a whole second install; ~30–45 min with ccache). The verifier
  runs it once in this change; the weekly slow sweep keeps it honest. The scratch clone uses the same ports, so it runs
  only when the main stack is down; it must restore nothing in the main checkout and must not touch the player's data.
- **F28:** route every harness-shaped `./play`/app run to a scratch play client (`HS_PLAY_CLIENT_DIR`) or refuse it;
  `client.stock_mode` and `play.loop_clean_exit` are the known offenders.
- **F30:** record the repo path the build and install were made for (the install record); compare at `hs up`/`./play`.
- Client-data and VMaNGOS sha256: compute from the pinned URLs once, record in fork.lock with the date.

## Open questions for the builder (answer in "As built")
- The minimum macOS version that the Wine runtime and our launcher actually need (cite the evidence).
- Whether a second macOS account can use the dev Mac's /opt/homebrew read-only (it should); document it for friends.

## As built (B29, 2026-10-07)

**Entry points.** `./bootstrap` (POSIX sh) finds a Python ≥ 3.10 (`python3`, else `/opt/homebrew/bin/python3`); with none it prints the
Homebrew line (and Homebrew's own install line when Homebrew is missing) and stops. It then execs `harness/hs bootstrap`, which is
`harness/hslib/bootstrap.py`. Options: `--yes` (install missing Homebrew packages without asking), `--check` (preflight plus each
step's done() verdict; changes nothing). The client argument is optional once `client/` is installed and intact. One bootstrap per
folder: a `flock` on the repo folder itself (so a refused run creates no file).

**Steps** (each `done()` is read-only and judges the output, never a marker alone):

| Step | done() | run() |
|---|---|---|
| preflight | always runs | arm64 (`sysctl hw.optional.arm64`, so a Rosetta-run Python still sees Apple Silicon), macOS ≥ 26.2, Rosetta (`arch -x86_64 /usr/bin/true`), CLT (`xcode-select -p` + `xcrun --find clang`), `/opt/homebrew/bin/brew`, the client (zip or folder; Wow.exe read straight out of it and its PE version must be 3.3.5.12340; a `Data/enUS/` folder), nothing of Moonglade running (stack processes or a held `play.lock`), free disk ≥ the sum of the remaining steps' needs + 1 GB |
| brew | `brew bundle check --no-upgrade --verbose` (fail-closed: an unparseable failure counts as missing) | lists what's missing, asks (or `--yes`), `brew bundle install --no-upgrade`, re-checks. Never upgrades (plain `brew bundle check` flags outdated formulae on the dev Mac; `--no-upgrade` everywhere) |
| build | binaries executable, install record names this folder, build stamp == install stamp == current input hash, vendor HEADs at their pins | `hs build --clean` |
| data | `downloads.client_data_state`: the unpack record names the pinned SHA-256, the 5 folders exist, and the file count and total size on disk equal the recorded ones | `downloads.install_client_data` (= `hs download client-data`): obtain (cache or download, verify), unzip into `data/client-data.unpack`, rename into place. A hand-made unpack (no record) is adopted only when every member of the verified zip is on disk with its size; otherwise moved aside, never deleted |
| database | the four `acore_*` folders exist with `.ibd` files and `data/.hs-db-ready.json` names this folder (written only after a first `hs up` reached ready and `hs down` succeeded) | `hs up` then `hs down`. Never deletes database files (they're the player's save); a failed first start says to ask before deleting anything |
| wine | runtime marker == the pinned SHA-256 and `wine` exists (wineinst's own test) | `hs wine-install` (now through the pinned downloader and the cache; unpacks into `wowsilicon-r17.unpack` and renames, so a killed install leaves nothing the next run trusts) |
| client | manifest present and `clientinst.mismatches(client/, manifest)` empty (sizes + binary hashes) | `hs client-install <client>`, then the registered `client.inspect` check in-process |
| playclient | `clientbuild.up_to_date()` (new, read-only: the same inputs/outputs comparison `build()` makes) | `hs client-build` |
| app | `app.up_to_date()` (new): `dist/app-stamp.json` (outside the bundle, so signature, bundle id and DR are unchanged) equals the current inputs (launcher.m, icon, repo path, python dir, macOS version, bundle id, DR, shim source), the launcher binary hash equals the stamp's, `codesign --verify --deep --strict` passes, the Dock shim is current | `hs app` (which now writes the stamp) |
| defaults | `world_size` and `client` set in `env/settings.json` | `settings.write_missing_defaults()`: the existing setters, only for unset keys |

Output: plain lines (`[i/10] label ...`, `already done (why)`, `done (time)`, and on failure `Setup stopped at step i/10: label.
Problem: ... What to do: ...`) plus one `[bootstrap] {json}` line per event, which the checks parse.

**Test switch.** `$HS_BOOTSTRAP_TEST` (JSON replacing what preflight observes: arch, macos, rosetta, clt, brew, brew_missing,
free_bytes, answer) is honoured only with the hidden `--allow-test-inject` **and** `$HS_HARNESS_RUN`; the env var without the
flag, or the flag outside a harness run, exits 2 before anything runs (`setup.preflight` proves both).

**Defaults in one place.** `settings.DEFAULTS` (world size normal, vanilla mode on, client patched). The getters' fallbacks
come from it; bootstrap writes the settings-file ones; `docs/setup.md`'s table between `<!-- defaults:begin/end -->` is
`settings.defaults_table_md()` verbatim (`setup.fresh_clone` compares them). Vanilla mode isn't a settings key: `hs vanilla-mode`
(new, show-only) reads `EraProfile.Enable` from the installed `server/etc/modules/mod_era_profile.conf.dist` (and any
population-profile override; none exists).

**Pinned downloads (F20).** `harness/hslib/downloads.py`: `PINS` maps each download to its fork.lock URL + SHA-256 key
(client-data, vmangos, wine-runtime, ghidra). fork.lock gained `client_data_sha256` (computed 2026-10-07 from the URL; its 22279
members match the dev Mac's `data/client-data` by size) and `vmangos_db_sha256` (a fresh download, 2026-10-07; equals the zip hash
already cited in the era SQL headers). A mismatch removes the file and raises before anything is unpacked. `$HS_DOWNLOAD_CACHE`
is honoured everywhere (not refused): every cached file is verified like a download, so it can't inject anything. `tools/era-diff`
now checks the VMaNGOS zip's pin before unpacking it. `hs download <name>` is the CLI. Ghidra keeps its own (already pinned and
verified) fetch.

**F30 moved repo.** `server/.hs-install.json` (the install record; not under `bin/` or `etc/`, so never part of the
verified-current digest) is written after every successful `hs build`. `build.moved_problem()` compares it (or, for a server built
before this change, `build/CMakeCache.txt`'s `CMAKE_CACHEFILE_DIR`) with the current folder. `hs up` (stack.up, before anything
starts) and `./play` (main, before the lock) refuse with the old folder, the new folder and `harness/hs build --force-clean`;
`hs build` wipes `build/` and the stamps first and builds from empty; `baseline.build_install` discards a foreign baseline build
tree (`discard_foreign_build`). Also: a failed vendor clone now raises a plain BuildError (no traceback), and an unfinished clone
left by a killed run is re-cloned.

**F28.** `play.prepare_play_client()`: when the play client is the player's own (`env/client-play/`, or the scratch named by
`$HS_TEST_OWN_CLIENT`) and the run has any harness sign, it is used as it is (`built: false`, `current` recorded) and a missing one
is refused; only a genuine launch calls `clientbuild.build`. `hs client-build` itself is unchanged (no check runs it without
`--dest`). The runner fingerprints the player's play client at the start of every run (`clientbuild.fingerprint`: folder inode;
SHA-256, inode and mtime of hs-build.json, Wow.exe and every stamped output).

**F33.** `runner.BUILD_CLAIM_ALLOWLIST` = {`core.login_logout_race_baseline`}; `build.clean` stays a whole-build check
(`BUILD_CHECKS`). Any other check's `evidence.build_s` counts as check time and adds a timing warning.

**Portability.** `repo.portable` scans `git ls-files --cached --others --exclude-standard` (working-tree content; binaries
skipped) for: `/Users/<x>`, `-Users-<x>`, `~` followed by a user name and a slash, this Mac's user name as a word, home folder, hostnames (`hostname`,
`scutil` ComputerName/LocalHostName), the repo's absolute path, the dev Mac's repo-folder convention (`Documents` + `/repos/`), and
an nvm install path. Fixed: the change-loop skill's nvm path, docs/setup.md, a variable named after the dev account in two checks (renamed
`player_cfg`), a sample `accountName` in client_checks. Allowlist (7 entries, each with its reason in `setup_checks.PORTABLE_ALLOW`):
STATUS.md (orchestrator-owned), tools/rename-folder.sh and the naming check that pins its lines, the archived rename change, the
easy-setup spec text (before and after archiving), and a report heading anchor naming the person.

**Kill-safety additions.** `clientinst.install` removes `*.hs-part` files a killed install left; wineinst and the server-data unpack
use temp folders renamed into place; the bootstrap lock is a flock (released by the kernel on SIGKILL).

### Answers to the open questions

- **Minimum macOS: 26.2.** `otool -l` on the pinned runtime (2026-10-07): `bin/wine`, `bin/wineserver`, `lib/wine/x86_64-unix/ntdll.so`,
  `winemac.so` and 25 more are built with `LC_BUILD_VERSION minos 26.2`; dyld refuses to load a binary whose minos is newer than the
  running macOS. (`libvulkan_kosmickrisp.dylib` says 26.5, but it's loaded only for Vulkan, which mtld3d's D3D9→Metal path doesn't
  use; 26.2 is the floor for wine itself.) The launcher and the Dock shim are built with `-mmacosx-version-min=13.0`; the app's
  `LSMinimumSystemVersion` is the building Mac's version (app.py), which is fine because each person builds their own app.
  Preflight enforces 26.2 (`bootstrap.MIN_MACOS`).
- **A second account and `/opt/homebrew`.** Permissions: `/opt/homebrew` `drwxr-xr-x`, `bin/` and `Cellar/` `drwxrwxr-x`, binaries
  `-r-xr-xr-x`, all owned by the installing account: readable and runnable by everyone. Evidence (no second account was created:
  that needs an admin password): with every write under `/opt/homebrew` denied by a `sandbox-exec` profile, `brew bundle check
  --no-upgrade` says "The Brewfile's dependencies are satisfied." and `cmake`, `mysqld --version`, `ccache` and `python3` run. So a
  second account can use the existing Homebrew read-only; it can't install into it (only the owner can run `brew bundle install`),
  and its first server build compiles from scratch (ccache lives in each account's `~/Library/Caches`). Documented in
  docs/setup.md step 1. The human-verify scenario (2.2) remains the real proof.

## Measurements (B29, 2026-10-07, M2 Pro 16 GB, macOS 26.6.2, `setup.fresh_clone`)

| | Cold compiler cache (run 1) | Warm compiler cache (run 3) |
|---|---|---|
| fresh `./bootstrap --yes` | 2179 s (36.3 min) | 435 s (7.3 min) |
| of which: server build (clone + compile) | 1935 s | 300 s |
| server data (from the cache: verify + unzip) | 33 s | 26 s |
| databases (first `hs up` + `hs down`, m0) | 135 s | 82 s |
| Wine runtime / client install (18 GB, folder source) / play client / app | 10 / 51 / 6 / 9 s | 5 / 17 / 2 / 2 s |
| re-run (every step "already done", no output changed) | — | 1.8 s |
| resume after SIGKILL at 4.3 GB into the client install | — | 30 s (client + play client redone; nothing earlier touched; no `.hs-part` left) |
| `./play` stand-in to "ready" (incl. the 120 s no-window fallback, first Wine prefix) | — | 238 s; resumed install 140 s |

Disk (`du`-style sums in the scratch clone): vendor 2.4 GB, build 1.4 GB, server 0.05 GB, data 4.4 GB (client-data 3.1 + MySQL),
client 17.9 GB, env 18.3 GB of which ~17 GB is the play client's copy-on-write clone (shares storage with client/): about **28 GB**
on disk. Runs 1 and 3 used the default `hs build -j 8`; bootstrap has no jobs option (it calls `hs build --clean`).

Real power-loss kill (2026-10-07 05:02): run 2 was killed mid server build (3 %). Left behind: `vendor/` complete and at the pins,
a partial `build/` with **no** input-hash stamp, no `server/`, nothing else. `./bootstrap --check` afterwards: build "to do (no server
binaries yet)" and every later step "to do"; a re-run would build from empty (no stamp = never trusted). No `.part`/`.unpack`
leftovers. The main checkout's MySQL had been shut down cleanly at 04:24, so no InnoDB recovery was needed; `hs up` (ready in 18 s) and
`hs down` (graceful) afterwards were clean.

## As built: pristine-client protection (B30, 2026-10-07, task 1.8)

**How the 08:22 incident happened (reproduced on scratch folders).** The makeshift copy's `client` was a symlink to the real
`client/`. `clientbuild.build` ran `cp -cR <base> <tmp>`; BSD `cp -R` copies a symlink named on the command line as the link
itself, so the staging folder *was* a link to the real `client/`. The build then ran `shutil.rmtree(tmp/<state folder>)` for
WTF, Screenshots, Cache, Logs and Errors (**this deleted the real `client/WTF` and the other state folders**: none is in the install
record, so `client.inspect` stayed green), patched `tmp/Wow.exe` (= `client/Wow.exe`) by `os.replace`, and only the MPQ writer's
own identity guard stopped it. Evidence that `client/WTF` existed before: the 01:49 and 07:41 runs' scratch builds record "carried
over (copied) from …/client/WTF"; at 10:4x it was gone.

**1. OS write protection** (`harness/hslib/pristine.py`). `lock()` removes every write bit and sets the user-immutable flag
(`chmod a-w` + `chflags uchg`) on every file and folder of the install, the folder itself last. Measured on APFS, macOS 26.6, as the
owner: overwrite (`>`, `>>`, `r+b`), `os.replace` onto a file, `cp` over, rename, delete, create (file, archive, folder), `rm -rf`,
`shutil.rmtree`, renaming the client folder and `chmod u+w` all fail (EPERM), both directly and through a symlinked path. `chmod a-w`
alone isn't enough: the folder itself could still be renamed and its entries deleted (counterexample run). Kept writable on purpose:
the game's own state folders `WTF/ Cache/ Logs/ Errors/ Screenshots/` (stock mode runs Wow.exe from `client/` and the game writes
them; `lock()` creates them empty when missing, since a locked folder can't get new entries) and mtld3d's `mtld3d_shaders.bin`. None
of them is in the install record. `client.stock_mode` passes on the locked client.
An APFS clone (`cp -cR`) inherits `uchg` on files and the read-only folders, so `clientbuild` clears both on its clone
(`make_writable`: new inodes, the source is untouched); `client.pristine_locked` proves the play client comes out writable, unflagged
and patched. `clientinst.install` runs inside `pristine.unlocked()`: it lifts the lock, installs, and re-locks in a `finally`, so also on
failure. A SIGKILL leaves the folder unlocked; the next `hs client-install` re-locks it, and bootstrap's client step `done()` reports
"installed, but not write-protected", so a re-run reinstalls (idempotent) and re-locks. `hs client-lock [--status]` locks or reports. The real `client/`
was locked with it on 2026-10-07 11:21 (Zach approved); `client.inspect` passes, 0 manifest mismatches.
Scratch trees holding a locked install are deleted with `pristine.remove_tree` (setup.fresh_clone's clone; `_flip_copy` unlocks its clone).

**2. Writer guards by identity.** `pristine.inside()` (moved from `mpq._under_client`, which now calls it) compares st_dev/st_ino of
every existing ancestor (resolved and lexical), so symlinks, `..` and case variants are caught. Before writing anything,
`clientbuild.build` refuses a destination, staging or trash path inside `client/`, the test stand-in or the base. It clones the base's
*real* folder (`realpath`) and checks the clone is a separate real folder (not a link, not the base's inode). `apply_binary` and the
add-on copy re-check their targets. The MPQ writer also checks every protected root. `clientcfg` (stock mode configures `client/` itself)
may write only `WTF/Config.wtf` and `Data/<locale>/realmlist.wtf` inside the pristine client. `frontmenu.write` keeps its own check.

**3. Per-run fingerprint.** The runner fingerprints the pristine client at the start: the folder inode; per file outside the state
folders its inode, size, mtime, flags and mode; the .exe/.dll SHA-256s; the install-record comparison; the lock state. `report.client_pristine`
(post-run, every run) compares it with the end and fails naming the changed paths. Its self-test (unfiltered runs and runs whose
filter names it; ~1 s) starts two nested `hs run`s whose root is a scratch stand-in (`$HS_TEST_PRISTINE_CLIENT`). Untouched, it passes.
With `$HS_TEST_PRISTINE_MUTATE`, a probe check (registered only under both switches) appends to one file, and it fails naming that file.
The switches are refused by `./play` and by every `hs` command except a nested `hs run` (`$HS_HARNESS_RUN` already set). The stand-in
can never be `client/` or a folder holding it.

**4. Agent hook.** `.claude/hooks/guard-client.sh` (POSIX sh + `python3 -I`, repo = its grandparent folder or `$CLAUDE_PROJECT_DIR`),
wired in `.claude/settings.json` as a PreToolUse hook for `Bash|Write|Edit|MultiEdit|NotebookEdit` (exit 2 + reason on stderr). It
blocks:
- a symlink-making command (`ln -s`, `os.symlink`, `symlink_to`) whose text names `client`, `env`, `data` or `server`;
- writes into `client/`: redirections, `tee`, `cp`/`rsync` destinations, `mv`/`rm`/`touch`/`mkdir`/`chmod`/`chflags`/…, `sed -i`,
  `dd of=`, extractors, writes after `cd client`, and inline-code write calls whose argument is a `client/` path;
- Write/Edit/NotebookEdit on a path inside `client/`.

Allowed: reads, git, `harness/hs`, `./play`, `./bootstrap`. Known false positives, because the rules match the command *text*: a
heredoc or echo whose text mentions a symlink command next to those folder names, or an inline write call to a `client/` path, is
blocked even when it only writes documentation (seen twice while building this). `repo.agent_guard_hook` feeds 51 samples since B32 (including
the literal 08:21 loop) and checks the wiring.

## As built: the two verifier FAILs of 2026-10-07 (B30, task 1.9)

**`play.offline` (fixed).** Wine's ping reaches `sendto()` and gets EPERM from the sandbox (WINEDEBUG `+nsi`: "sendto() rets -1 errno 1"),
so the deny works. What changed after the 05:05 reboot is the kernel's *logging*. For non-Apple (ad-hoc signed) processes, the first
violation report is usually not emitted at all (not in `log stream` either). Only the later "N duplicate reports for Sandbox: <proc>(pid)
deny(1) …" summary appears, and only when the process repeats the denial and lives a moment longer. No first report in 10+ tries at
10:2x for Homebrew Python, a freshly compiled ad-hoc binary or winedevice.exe; always one for Apple's `/sbin/ping` and CLT Python. Self-test A only passed
because its IPv4+IPv6 connects to :443 made a duplicate summary. This is outside our control: no SBPL modifier changes it (`(with report)`
is refused for deny rules, and `(debug deny)` has no effect). The self-test now sends 4 echoes and reads the log while the prefix still
runs, then stops it. The requirement is unchanged: a kernel log line naming a `.exe` with `deny network-outbound`, non-DNS (`_LINE_RE`
matches it inside a duplicate summary). 3/3 pass (one even got a first report); with 1 echo it fails again. Caveat for the loop window: a
*single* denied attempt by the loop could now go unlogged; the deny itself is enforced either way.

**`client.wording_addon` (not fixed: diagnosis only).** easy-setup's diff is not involved: the run builds a scratch play client, so the F28
path takes the old `clientbuild.build` branch unchanged, and HS_TEST_OWN_CLIENT is unset. Two different failure modes:
- *Since 08:22* the scratch play client gets no settings, because the incident deleted `client/WTF` (builds carry it over). The intro
  movie plays and the TOS/EULA screens are pending (`movie`, `readTOS` and `readEULA` were in it), so the front menu never logs in ("no add-on
  report"). The checks silently depended on that undeclared `client/WTF` state.
- *07:41–08:19* (with `client/WTF` present; reproduced by seeding those three settings into the scratch): the character enters the world
  and leaves it 0.07–0.4 s later by an orderly logout. connection.log shows a second `COP_LOGIN_CHARACTER … TRUE`, then `COP_GET_CHARACTERS`;
  the server log has Login and Logout in the same second; the add-on records `LOGOUT_CANCEL` and `PLAYER_LEAVING_WORLD`, then saves.
  Ruled out: the add-on (no Lua `Logout`/`Quit`/`ForceQuit` call, no `StopCinematic`; the same happens with the test hook not planted),
  the harness (its SIGTERM comes after the save), and focus or keys (Wine `+event,+keyboard`: no key, close or activation event). The cause is
  still open. Candidates: server or DB state of the test account or character since the 01:49 pass (account re-created, character level 10,
  `account_access` row).
  The add-on's test hook now records LOGOUT_CANCEL, PLAYER_LEAVING_WORLD, PLAYER_CAMPING, any Logout/Quit caller, its own
  StopCinematic calls and the state at logout, so the next failure shows this directly.

## As built: `client.wording_addon` fixed (B31, 2026-10-07, task 1.9 remainder)

**Failure mode A: settings the scratch client silently took from client/WTF.** Every scratch play client a real-client check
builds now gets its preconditions from the harness, never from `client/` (`playclient_util.build_scratch` /
`seed_settings`; `PlayRun.start` seeds an existing or not-yet-built scratch itself):
- the WTF folder the build carried over from `client/WTF` is dropped, and `WTF/Config.wtf` gets `movie "0"`, `readEULA "1"`,
  `readTOS "1"` (without them the intro movie, then the EULA and Terms of Use screens come before the login screen:
  `AccountLogin_ShowUserAgreements` in the client's GlueXML; the other notices are off in enUS, `GlueLocalization.lua`),
  `hwDetect "0"` (measured: with no saved settings the client's first-start hardware detection replaced the video settings,
  so `client.resolution_default`'s back buffer stayed 1024x768 despite the written gxResolution; with it, it passed) and
  `realmName` = realm 1's name (measured: without it `login.friendly_errors`' Try again sat on the realm list polling
  `COP_GET_REALMS`, and the seamless run's first Continue ended in an error before a retry reached character select);
- the test account's empty `WTF/Account/HSTEST/SavedVariables.lua` and `…/<realm>/Hstester/SavedVariables.lua` (a first login
  logs "Error loading …SavedVariables.lua" into FrameXML.log, which the checks require to be empty; the old `client/WTF`
  held them). The realm name comes from `auth.realmlist` when MySQL is up, else from AzerothCore's base SQL
  (`data/sql/base/db_auth/realmlist.sql`; the harness never renames the realm).
- `PlayRun.start` requires these settings before launching: "GIVEN not met: the scratch play client's WTF/Config.wtf lacks
  movie "0", readEULA "1", … " (counterexample: seeding disabled, fails in 2 s). If the login screen still never
  comes up (screens pending despite the guard), `client.wording_addon` says so: no screenshot shows the front menu's marker, with
  the settings at launch (counterexample: opposite values seeded, guard bypassed).
- Call sites moved to `build_scratch`: the seamless play and error sessions, the login continue session, `client.resolution_default`'s
  e2e (its "fresh scratch has no resolution" GIVEN depended on client/WTF too), `app.shim_perf`'s scratch. The backdrop and
  stock-mode scratches are built by `PlayRun.start`. Stand-in runs (backups, F28) never start the real client.

**Failure mode B: the client's own idle logout.** The client sends `CMSG_LOGOUT_REQUEST` itself, right after its login burst
(server opcode trace, `Logger.network.opcode` at trace, temporary: `…CMSG_TUTORIAL_FLAG, CMSG_VOICE_SESSION_ENABLE,
CMSG_LOGOUT_REQUEST`), not through Lua (the add-on's Logout/Quit hooks saw no call). Read-only disassembly of the env/re copy
of Wow.exe 12340 (llvm-objdump; sha256 aa63a575…): the in-world idle check at 0x0052b24c takes the millisecond clock
(0x0086ae20; GetTickCount/timeGetTime, i.e. since the Mac booted), subtracts the last hardware input time (0x00b499a4,
zero-initialised, written only by the keyboard/mouse event handlers at 0x004943e2…0x004954ad), compares it *signed* with
5 and 30 minutes, and at 30 minutes raises IDLE_MESSAGE and calls the logout routine 0x006b1930 (the one Lua `Logout()`
reaches via 0x00510430). The harness drives the client only through the GlueXML test hook (Lua clicks are not hardware
input), so the client's "idle time" is the Mac's uptime. Before the 05:02 power loss the Mac had been up 37.8 days
(since Aug 30): more than 2^31 ms, so the signed difference was negative and the check never fired; that is why every run
up to 03:35 passed. After the reboot the uptime was 2-7 h: every world entry ended 0.07-0.4 s later (the second
`COP_LOGIN_CHARACTER` line is the client's own logout path, then `COP_GET_CHARACTERS`). Not the cause: easy-setup's diff
(no client-path change since main's 438d6df), the test account or character (a freshly re-created level-1 character fails
the same), DB updates (none since), the screen lock (08:15 run failed with the screen unlocked and the display on).
Causal test: one Shift press sent into the client's prefix after its window was up → the character stayed, the popup was
shown and read, the add-on's own Logout followed (3/3 since).
- Fix (`checks/clientinput.py`): a 1 KB PE32 program written byte by byte in Python (no Windows toolchain), run with the
  prefix's own wine: `SendInput` with VK_SHIFT down/up, exit code = events injected. The seamless play session runs it once
  the front menu's first screenshot exists and requires 2 injected (GIVEN). Shift alone does nothing in the glue screens
  or the world. Only that session (the one that must stay in the world) uses it; `login.auto_login`'s "no keystrokes
  injected" run does not.
- `client.wording_addon` now also fails, with that diagnosis, when PLAYER_LEAVING_WORLD comes before the add-on's own Logout
  call (counterexample: press skipped, guard told it worked). Counterexample for the guard: SendInput injected 0.
- A real player is not affected in practice: any key or mouse input resets the timer; the client's behaviour is unchanged.

## As built: B32 (2026-10-07)

**`play.offline` self-test B made deterministic.** The 14:02 FAIL (1 in 7): Wine's ping was blocked ("General failure") but
the log had no line, because winedevice.exe exited (the prefix shut down after the 4th echo) before the kernel's
"N duplicate reports" summary was emitted (`wineserver -k` exit 1 = no server left). Now `_selftest_wine` starts
`ping -n 60` under the profile and polls `/usr/bin/log show` (predicate: this run's unique tag) every 2 s while it runs;
as soon as a `.exe` non-DNS deny line appears it stops the prefix. Bound 75 s. Fail-closed: no line within the bound, or
any "Reply from 1.1.1.1" in the ping output, fails. The prefix's pids during the self-test are excluded from the loop's
denial window (a late summary for a self-test process can't count against the loop). Measured: line seen after 3-6 s in
19/19 runs (11 standalone + 8 `play.offline`), first-report or duplicate-summary. Counterexample: the profile without the
deny rule → 60 replies, no line, FAIL after 63 s.

**Agent hook.** `sh|bash|zsh|dash|ksh|fish -c '<code>'` (also `-lc`, `-ec`, after `env`/`sudo`/...) and `eval <words>`
get their inner code checked with the same rules (recursively, depth 4). `find` left the read-only list: with a start path
in `client/` (or relative after `cd client`), `-delete`, `-exec/-execdir/-ok/-okdir` of a command that isn't read-only,
and `-fprint*` into `client/` are blocked; `find client -name x` and `-exec shasum {} \;` pass. 10 blocked + 7 allowed rows
added; the previous hook lets all 10 new blocked rows through. Known gap: `bash script.sh` (a file the hook can't see),
`find . -delete` from the repo root (the OS lock still refuses it).

**GUI preflight.** `hs run` with no `--only` and tier full/slow/gui checks `guienv.probe()` (screen lock via ioreg,
`CGDisplayIsAsleep`, `pmset -g batt`) and refuses in < 1 s with one line and exit 2 when the screen is locked, the display
asleep, or the Mac is on battery below 35 %. `--ignore-gui-preflight` overrides. Unknown probe values don't refuse.
Filtered, quick and backups runs never refuse (nested runs inside checks are all quick).

**`report.timing`.** Its own evidence called the run's build total `build_s`, which `build_claim_warnings` then read as a
claim by `report.timing` ("claims 30 s ... not on the allowlist"). Renamed to `run_build_s`; synthetic case (g) proves
report.timing's own record over a 420 s-build run raises no warning, while case (e) still warns for a real check's claim.

**Scratch env.** `setup_checks.scratch_env` drops `HS_TEST_PRISTINE_CLIENT`, `HS_TEST_PRISTINE_MUTATE` and any `HS_TEST_*`.

**Manifest `client_dir`.** Written repo-relative (`"client"`) by `clientinst.manifest_client_dir`; an install outside the
repo stays absolute. A no-op reinstall heals an old value (`heal_manifest_client_dir`: rewrites only that field of the
manifest, never opens `client/`); the dev Mac's manifest was healed with that function (it named the pre-rename
old project folder). `client.inspect` now requires the field to be current.

**Friend first launch (item 6).** Confirmed: a play client built from a `client/` without saved settings (a friend's: the
lock creates an empty `client/WTF`, stock mode adds only realmlist/patchlist/gxWindow/gxApi) starts with the intro movie,
then the EULA/TOS screens, at a 1024x768 window from hardware detection, and never reaches the front menu or the character
list without input (measured: counterexample run, no menu marker in 2 screenshots, both 1024x768, no character list in
90 s). `clientbuild.build` now adds `movie "0"`, `readEULA "1"`, `readTOS "1"`, `hwDetect "0"` and `realmName` (auth DB if
up, else AzerothCore's base SQL) to `WTF/Config.wtf` **only for keys not set yet** (`clientcfg.add_missing_settings`, never
inside `client/`), after the state folders are moved in, so an existing play client's settings are never changed; an
up-to-date play client isn't touched at all. readEULA/readTOS are seeded because B30/B31 measured that those screens come
before the login screen (`AccountLogin_ShowUserAgreements`); for Zach to confirm: this accepts the client's EULA/TOS
screens on the player's behalf, and skips the intro movie. New check `client.first_start_settings` (full tier, real client,
~70 s): build exactly as `hs client-build` does, the five settings present; the front menu is the first screen; one Continue
reaches the character list; the window has the configured resolution's aspect ratio. Not yet mapped to a spec scenario
(orchestrator: add one to play-client). Zach's `env/client-play` already has all five; it was byte- and stat-identical
before/after this work.

**Idle logout and real players (item 7).** No path enters the world without player input: `EnterWorld()` in the glue
add-ons runs only under `HS_TEST` (the test hook's `enterworld` act and `enter_world_after_charlist`), and the front menu
waits for a Continue click (`login.fresh_menu_waits`), then the player clicks Play. Those clicks are hardware input, which
resets the client's idle timer, so a real player gets the stock 30-minute AFK logout only after 30 minutes without input.

## As built: B33 (2026-10-07)

**`client.first_start_settings`: "settings already set are left unchanged".** New structural sub-case
(`login_checks._preset_settings_kept`, runs right after the first clause, before the client launches): an APFS clone of
`client/` (read-only use; `pristine_checks.clone_client`) in `env/check-play-firststart-base` gets a `WTF/Config.wtf` of
`SET gxWindow "1"`, `SET movie "1"`, `set READEULA "0"` (CRLF, mixed case, values opposite to the defaults); a play client is
built from it with `clientbuild.build(base=...)`. The built file must start with those bytes exactly, `movie`/`readEULA` keep
`1`/`0`, and exactly `readTOS`, `hwDetect`, `realmName` are appended (CRLF), nothing for the keys already set. Both scratch
folders are removed. Counterexample: `add_missing_settings` replaced by an overwrite (`clientcfg._update_config`) → FAIL
("the base's Config.wtf isn't a byte-for-byte prefix").

**Agent hook.** Three more ways into a shell are covered: shell options that take a value before `-c` (`-o pipefail`,
`-eo pipefail`, `+o nomatch`, `-O`/`+O`, `--rcfile`, `--init-file`); a here-string (`bash <<< '<code>'`, whose code gets
the same rules); and a shell that reads its code from stdin (`... | sh|bash|zsh`, `bash -s`, a heredoc, `bash < file`),
which is blocked when the command's text names `client/` (the piped text isn't parsed; a read-only pipeline that names
`client/` and feeds a shell is blocked too, a known false positive). 10 blocked + 5 allowed rows added to
`repo.agent_guard_hook` (now 66 rows); the previous hook lets all 10 new blocked rows through. Left alone on purpose:
obfuscation such as `$(echo cp) x client/Wow.exe` or a variable holding the verb; the OS write protection of `client/`
(`hs client-lock`) refuses those writes anyway.

**`hs run --results-dir` outside the repo.** `full_run_record`'s `outputs` used `relative_to(P.ROOT)`, which raised for a
results folder outside the repo; it now falls back to the absolute path (`reporting._repo_rel`).
