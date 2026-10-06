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

## Data provenance (§7)
No game data values. The stock strings being replaced are read from the installed client (GlueStrings/GlobalStrings),
never typed from memory.

## Spec-change log
(none yet)
