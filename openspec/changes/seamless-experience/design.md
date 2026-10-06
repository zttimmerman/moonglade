# Design: Seamless experience

## Approach
- **GlueXML (patch 0002 data):** extend `client-mods/glue/HSFrontMenu.lua/.xml`. Hook `GlueDialog_Show` (or the
  equivalent in the installed client's GlueXML; read it, don't assume) so that, while the overlay is active, the stock
  connection dialogs (keys such as CONNECTING, AUTHENTICATING, LOGIN_SUCCESS and the character-list dialog; take the real
  keys from the client's GlueDialogs) aren't shown and the overlay is. Error dialogs are mapped to friendly text with
  Try again / Back to menu / Show details. Character select: hide `CharacterSelectRealmName` and the Change Realm button,
  relabel `CharSelectEnterWorldButton` and the back button (take the real frame names from the client's GlueXML).
- **Server text:** worldserver config (e.g. `Motd`) and module settings, written by `harness/hslib/config.py` like
  other overrides. Find every login-time message source in the pinned core and modules; cite them in "As built".
- **Wording add-on:** `client-mods/addons/Moonglade/` (`Moonglade.toc` + Lua), copied into the play client's
  `Interface/AddOns/` by the client build (a data patch through the ADR-0009 queue, e.g. 0003, or the build step; the
  builder picks one and records why), enabled for the player's account. Replaces global strings at runtime. Test
  output goes through SavedVariables, written only when the test hook is on.
- **Launcher errors:** `launcher.m` dialog text: friendly main text plus a details disclosure (NSAlert accessory
  view, or a "Show details" button that expands).
- **Restore feedback:** the Backups window's status line after `./play --backups restore` returns.
- **F29:** rename the allowlisted old-name lines in the patch inputs and remove them from the naming allowlist
  (`repo.naming` must then pass with a shorter allowlist).

## Open questions for the builder (answer with evidence in "As built")
- Exact dialog keys and frame names in the installed 3.3.5a GlueXML (read the client's files via `harness/hs mpq`).
- Whether hooking those dialogs leaves the login state machine intact (cancel, timeouts, errors).
- Which login messages the server and modules send today (MOTD, AzerothCore's server info line, playerbots messages).

## Measurements
Builder B26, 2026-10-06, on the m0 stack, real Wow.exe (scratch play clients), the headless probe.
- **Login timing:** Continue to character list 3-4 s locally (overlay phases of 65-83 frames). With the login server
  stopped, the failure arrives in the same frame as the Continue (the overlay phase is 1 frame): the client's
  connection.log shows `LOGIN_STATE_FAILED result: LOGIN_SERVER_DOWN` then `LOGIN_FAILED`, 3 ms after connecting.
- **What the stock client opens while logging in:** only `OPEN_STATUS_DIALOG("CANCEL", <text>)` progress dialogs (4-6
  per login, intercepted), with `UPDATE_STATUS_DIALOG` text changes and `CLOSE_STATUS_DIALOG`. A login with the login
  server down opens `CONNECTION_HELP_HTML` with GlueStrings `LOGIN_FAILED` (316 bytes of HTML).
- **CHARACTER_LIST_UPDATE fires twice** after a login: first with 0 characters, then with the list.
- **Console variables hold at most 255 characters:** a longer accountList value is cut at 255 in WTF/Config.wtf.
- **DefaultServerLogin wipes the password string it is given, in place.** A second login in the same glue session
  (Try again; Main Menu then Continue) failed with LOGIN_ENTER_PASSWORD ("Please enter your password."), because Lua
  strings are shared and the profile's own copy was the wiped one. Fixed in HSFrontMenu_Login (bytes kept, a fresh
  string per login); this also fixes Main Menu -> Continue, which was broken since in-client-login.
- **Login text today (probe, 30 s in the world, Moonglade.QuietServerText = 0):** SMSG_MOTD with 2 lines,
  "Welcome to an AzerothCore server." (auth.motd) and "|cffFF4A2DThis server runs on AzerothCore|r ..." (hard-coded in
  MotdMgr::CreateWorldPacket); two CHAT_MSG_SYSTEM lines from mod-playerbots, "This server runs with mod-playerbots
  https://github.com/mod-playerbots/mod-playerbots" and "Playerbots: The server is configured with 10 bots.". With
  QuietServerText = 1: no text packet at all in 30 s.
