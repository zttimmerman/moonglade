# Proposal: Seamless experience (front menu step 3)

## Why
Zach's principle (2026-10-05): Moonglade should feel like one single-player game. Nothing on screen should say
server, realm, connecting or account unless the player opens a details view. Today, after Continue, the stock client
shows "Connecting", "Authenticating", "Success!" and "Retrieving character list"; character select shows the realm
name and a Change Realm button; the server sends a message of the day; and errors show raw protocol wording.

## What Changes
- **Loading overlay:** after Continue, a themed "Entering Azeroth…" overlay replaces the stock connect, authenticate and
  character-list dialogs until character select appears.
- **Character select:** the realm name and Change Realm are hidden; Enter World reads **Play** and Back reads **Main Menu**.
- **Server-sent text:** the message of the day and any login announcements from the server or modules are off, or
  rewritten without server wording.
- **In-game wording:** a Moonglade add-on, shipped in the play client as data and enabled by default, renames
  server-flavoured strings at runtime (e.g. Logout → Exit to Menu, "Disconnected from server" → game terms). No
  FrameXML file is changed. A real FrameXML change would need its own ADR and Zach's yes.
- **Friendly errors:** a login or connection failure on the front menu shows a plain message with **Try again** and
  **Back to menu**, plus a small **Show details** that reveals the real error. The launcher's error dialogs follow
  the same pattern, with the log path under details.
- **Restore feedback (Zach, 2026-10-06):** after a restore, the Backups window says clearly that it's done:
  "Restored. Your save is now from <date>."
- **F29:** rename the remaining old-name lines in the client patch inputs (patch 0002's title and similar).

Not in this change:
- A Saves entry in the in-game menu. It needs a way for the login screen to ask the launcher for a restore, which the
  client doesn't have today, so it gets a research spike and its own change (`front-menu-saves`).
- "Skip straight into the world", which stays a later option.

## Capabilities
### Modified Capabilities
- `login-screen`: loading overlay, character-select wording, friendly errors.
- `client-connectivity`: no server-sent identity text; the in-game wording add-on.
- `mac-app`: friendly launcher errors with details; restore feedback in the Backups window.
- `harness-reporting`: the full-run timing guard catches real slowdowns only (per-check baseline).

## Data provenance (§7)
No game data values. The stock strings being replaced are read from the installed client (GlueStrings/GlobalStrings),
never typed from memory.

## Spec-change log
- 2026-10-07 (clarification, from verifier cycle 1): "the disconnect and lost-connection messages" in the wording add-on requirement now names the strings that exist in the in-world interface (IDLE_MESSAGE, ERR_COMSAT_DISCONNECT). B26 measured that FrameXML has no world-disconnect string: a disconnect returns to the glue screens, where the friendly error covers it. Not a loosening: the disconnect wording is still required, under login-screen.
- 2026-10-07 (Zach's choice; **changes a guard, review at the M4 gate**): verifier cycle 1 FAILed `report.timing` (wall minus backups 2411 s against 1867.5 s). Measured against the save-backups verifier run: the 115 common checks were unchanged except the two that rebuild server binaries (`build.clean` +195 s from --force-clean; `core.login_logout_race_baseline` +164 s from its baseline rebuild), plus 229 s of new checks. The old guard (total wall against one fixed number) failed any change that adds checks or touches build inputs. Zach chose "catch real slowdowns only": a per-check baseline; build time and new checks are reported, not failed; a new check over 180 s is a warning.
