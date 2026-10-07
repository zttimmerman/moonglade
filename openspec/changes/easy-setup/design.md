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
skipped) for: `/Users/<x>`, `-Users-<x>`, `~name/`, this Mac's user name as a word, home folder, hostnames (`hostname`,
`scutil` ComputerName/LocalHostName), the repo's absolute path, the dev Mac's repo-folder convention (`Documents` + `/repos/`), and
an nvm install path. Fixed: the change-loop skill's nvm path, docs/setup.md, a `zach` variable name in two checks (renamed
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
