# Proposal: Easy setup (bring your own client, one command)

## Why
Zach wants to share Moonglade with friends (2026-10-06): a friend supplies only their own WoW 3.3.5a client, runs one
command, and ends up with a setup like Zach's. Today `docs/setup.md` is about ten manual steps, one of them a hand
download with no checksum. Easy setup comes before M4 (Zach, 2026-10-06) and is a prerequisite for going public.

## What Changes
- **`./bootstrap <client.zip|folder>`** (wrapping `harness/hs bootstrap`): one command from a fresh clone to a ready
  `dist/Moonglade.app`. It does:
  1. a preflight (Apple Silicon, macOS version, Rosetta, Xcode Command Line Tools, Homebrew, disk space);
  2. Homebrew dependencies from a committed **Brewfile** (shows what it will install and asks, unless `--yes`);
  3. the pinned sources, the server build and the server data download (now sha256-pinned);
  4. the database and first start, the Wine runtime, your client install and check, the play client, and the app.

  Every step is idempotent and resumable: re-running skips what's done, and an interrupted run continues where it stopped. Progress is shown in plain language, and a failure names the step, the reason and what to do.
- **Same setup as Zach's:** the defaults Zach plays with (vanilla mode on, the normal world size, the patched front
  menu), documented in one place.
- **README quick start:** install Homebrew, clone, `./bootstrap ~/Downloads/YourClient.zip`, open Moonglade.
  `docs/setup.md` stays as the detailed reference, rewritten around bootstrap.
- **Portability:** no tracked file depends on Zach's machine (home paths, user name, his folders), hooks included.
- **F28:** harness runs never rebuild the player's own play client (it happened three times on 2026-10-06/07).
- **F30:** a server built in another folder (repo moved or renamed) is detected with a clear "rebuild" message, and
  the dev-only baseline build heals itself.
- **F33 (part):** only allowlisted checks may claim build time in the timing guard.
- **F20:** pin the VMaNGOS download's sha256 too, alongside the client-data pin.
- **Pristine-client protection (added 2026-10-07, Zach: "this should never happen again"):** `client/` is write-protected
  at the file-system level, every client writer refuses it by identity, every run fingerprints it, and an agent hook blocks
  linking/writing the project's client and runtime folders.

Not in this change: Windows packages and releases (M5 item 8), going public (the `go-public` change).

## Capabilities
### New Capabilities
- `easy-setup`: bootstrap, preflight, idempotence, the same defaults, portability.
### Modified Capabilities
- `play-launcher`: the player's play client is never rebuilt by a harness run (F28).
- `server-build`: moved-repo detection; pinned data downloads (F30, F20).
- `harness-reporting`: build-time allowlist for the timing guard (F33).
- `play-client`: the pristine client can't be written (OS lock, writer guards, per-run fingerprint, agent hook).

## Data provenance (§7)
No game data values. Downloads are pinned by URL + sha256 in fork.lock.

## Spec-change log
- 2026-10-07 (addition, tightening): new `play-client` requirements for pristine-client protection. Reason: at 08:22 the
  verifier's ad-hoc A/B copy (scratch tree with `env/`, `client/` symlinked to the real ones) ran
  `client.wording_addon` on main's code; the play-client build resolved into `client/` and replaced `client/Wow.exe` with
  the patched one (the MPQ writer refused, the Wow.exe writer had no guard). Restored from `env/re/bin/Wow.exe`
  (manifest sha256 aa63a575…) with Zach's OK at 10:19; the patched file is kept at `env/tmp/Wow.exe.patched-set-aside-0822`.
- 2026-10-07 (test method change, for review at the next human gate): the seamless play session behind
  `client.wording_addon` now sends one hardware Shift key press into the client's Wine prefix after the front menu is up
  (`harness/hslib/checks/clientinput.py`). Reason (B31): the 3.3.5a client's 30-minute idle logout compares
  milliseconds-since-boot minus the last-real-input time as a signed 32-bit value (RE of the `env/re` copy, 0x0052b24c →
  0x006b1930); the harness never sends real input, so with Mac uptime between 30 min and ~24.8 days the client logs
  itself out within a second of entering the world. Every pass before 05:02 was on a 37.8-day uptime. The only
  alternative is a client binary patch (ADR + Zach's yes). `login.auto_login` still injects no keystrokes; no scenario
  text changed. Also: scratch play clients no longer copy `client/WTF`; each seeds `movie`, `readEULA`, `readTOS`,
  `hwDetect`, `realmName` and the test account's SavedVariables itself and refuses to launch without them.
