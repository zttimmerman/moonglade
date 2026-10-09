## Why
Prebuilt releases, part 2 of 3 (STATUS "Next", Zach 2026-10-07; split in `release-build`'s proposal). `release-build` makes a relocatable payload (server, MySQL, Python, harness, SQL) that runs without Homebrew, but nothing in it can play yet: the harness still assumes a git checkout, writes into the repo (`env/`, `data/`, `client/`), runs Homebrew's `mysqld` and the dev `vendor/` source tree, and the only way to set up is `./bootstrap` in a terminal. A friend who downloads the release needs to double-click Moonglade, point it at their own 3.3.5a client folder (bring your own client, ADR-0012) and end up playing, with no terminal, Homebrew or Xcode tools.

## What Changes
- **Two places, one resolver.** The app and everything shipped in it (APP) is read-only and can be moved, sit on a read-only volume or be translocated by Gatekeeper. Everything the friend creates (HOME = `~/Library/Application Support/Moonglade`) holds the MySQL data, rendered server configs and logs, server data (client-data), the Wine runtime and prefix, Moonglade's copy of their client and the play client derived from it, settings, `player.json` and backups. `harness/hslib/paths.py` resolves every path for both modes. A git checkout is always dev mode with today's layout, byte for byte.
- **Release mode in the harness.** Release servers get module keys as `AC_*` environment variables (ADR-0011's lane-B mechanism; the compiled-in config dir isn't usable from a read-only app). `SourceDirectory` and `MySQLExecutable` point into the payload (and `OPENSSL_CONF` is set), as `release-build`'s "As built" requires; the DB updater reads the payload's `sql/`; `mysqld`, `mysql` and `mysqldump` come from the payload's `mysql/`; Python is the payload's.
- **Release app shape.** The release zip's top level becomes `Moonglade.app`, with the `release-build` payload inside it at `Contents/Resources/Moonglade/` and the prebuilt launcher as `Contents/MacOS/Moonglade`. Signing, the icon tile and the Info.plist "not for redistribution" line stay in `release-signing` (3/3).
- **First-launch window** (in the launcher's existing loading-window style). On a first open it: checks this Mac (macOS, Apple Silicon, Rosetta, free disk up front), asks for the friend's own client folder (or zip), checks it is 3.3.5a build 12340 with the existing client inspection, copies what Moonglade needs into HOME (never writing to the folder they gave), downloads the pinned Wine runtime and server data with progress, sets up the databases, builds the play client, and finally tells them they can delete the folder they gave it. It is resumable and kill-safe, and every failure is a plain-language message with what to do.
- **Measurement.** The databases' first start is timed and split into SQL import vs bot setup, and recorded in design.md. No prebuilt database template is decided here.
- Harness checks for all of the above, run against a scratch HOME and a copy of `env/release`'s payload, under `release-build`'s no-Homebrew sandbox profile.

## Capabilities
### New Capabilities
- `release-runtime`: the APP/HOME split and release mode in the harness and launcher.
- `first-launch`: the first-launch window and its setup steps.

### Modified Capabilities
- None. `release-build`'s requirements are unchanged; its checks follow the payload's new root inside `Moonglade.app` (a path change, not a loosening; listed in tasks 1.3).

## Impact
- `harness/hslib/paths.py` (mode + HOME), `config.py`, `stack.py`, `downloads.py` (progress), `bootstrap.py` (release step list), `play.py`, `backups.py`, `settings.py`, `wineinst.py`, `clientinst.py`, `clientbuild.py`, `launch.py`: they take paths from the resolver instead of `P.ROOT`/`P.BREW`.
- `harness/app/launcher.m`: release mode (finds the payload from its own bundle, runs the payload's Python) and the first-launch window.
- `tools/release/release.py`/`package.sh`: the payload goes inside `Moonglade.app`.
- New checks `release.*` (runtime) and `firstlaunch.*`. Dev install, the player's data and the live databases are untouched.
- No game data values change (§7 not engaged).

## Spec-change log
- 2026-10-08 (clarification, orchestrator decision 1): "no characters" in the safe database reset means no *player* characters. Every first start creates the AI players' own characters on their `rndbot*` accounts (playerbots.conf.dist `RandomBotAccountPrefix`), so an interrupted first start always has some; they don't count, and a character whose account is missing counts as a player's. The count is taken on an APFS clone of the datadir by a throwaway mysqld, never on the datadir itself. The requirement text and the "datadir with characters" scenario's GIVEN now say "player's character". Not a loosening: what a player made is still never reset.
- 2026-10-08 (clarification, orchestrator decision 2): the disk check runs in two places. Before the folder pick it checks room for the fixed part (the client's size isn't known yet); right after the pick, before anything is copied, it checks the full need (client size + fixed part). The requirement text says so; the scenarios are unchanged.
- 2026-10-08 (clarification, orchestrator decision 3): Moonglade's copy of the friend's client in HOME (`HOME/client`) keeps its write protection in release, like the dev `client/`. Removing Moonglade is documented in `docs/setup.md` ("Using a release"): Finder asks to confirm deleting locked items, or one Terminal line lifts the protection first. No spec text changes.
- 2026-10-08 (builder c, wording): the "can't read the folder" message now also says what Moonglade needs (3.3.5a, build 12340, English), as the "wrong clients are refused" requirement asks of every refusal; the "download damaged" message starts with a capital letter.

## Open questions resolved (orchestrator, 2026-10-08)
1. A zip of the client is accepted as well as a folder (same as `./bootstrap`).
2. Final bundle id and the payload's place inside the app: left to `release-signing`.
3. Downloads resume per step, not per byte, for now; per-byte resume goes to the backlog if friends hit it.
4. Updating an installed release, an uninstall/reset button and choosing the data folder's location: backlog, not this change.
5. Checks clone from `client/` with `clonefile` into a scratch HOME only (independent copies, never symlinks or hard links, per the 2026-10-07 pristine-client incident); `report.client_pristine` must stay green on every run.
