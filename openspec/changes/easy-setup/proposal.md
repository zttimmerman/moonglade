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

Not in this change: Windows packages and releases (M5 item 8), going public (the `go-public` change).

## Capabilities
### New Capabilities
- `easy-setup`: bootstrap, preflight, idempotence, the same defaults, portability.
### Modified Capabilities
- `play-launcher`: the player's play client is never rebuilt by a harness run (F28).
- `server-build`: moved-repo detection; pinned data downloads (F30, F20).
- `harness-reporting`: build-time allowlist for the timing guard (F33).

## Data provenance (§7)
No game data values. Downloads are pinned by URL + sha256 in fork.lock.

## Spec-change log
(none yet)
