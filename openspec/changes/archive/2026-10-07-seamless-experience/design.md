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

## As built
Builder B26, 2026-10-06.

### Front menu (patch 0002; `client-mods/glue/HSSeamless.lua`, frames in `HSFrontMenu.xml`)
- **Dialog keys and frame names (answer to open question 1)**, all from the installed client's GlueXML
  (`Interface\GlueXML\GlueDialog.lua`, `GlueParent.lua`, `CharacterSelect.xml/.lua`, `GlueStrings.lua`, read with
  `harness/hs mpq`): the C++ side reports login progress with the events `OPEN_STATUS_DIALOG` (key `CANCEL`),
  `UPDATE_STATUS_DIALOG` and `CLOSE_STATUS_DIALOG`; `GlueDialog_OnEvent` hands `OPEN` to the global `GlueDialog_Show`.
  Seen in a login (test hook, on screen): `CANCEL ""`, `CANCEL "Validating Version"`, `CANCEL "Success!"`, updates
  "Connected", "Authentication Successful", `CANCEL "Retrieving character list"` (shown by `CharacterSelect_OnShow`
  -> `GetCharacterListUpdate`, i.e. already on character select), "Character list retrieved", close. Failures use
  `OKAY`, `OKAY_HTML`, `OKAY_HTML_EXIT`, `OKAY_WITH_URL`, `CONNECTION_HELP`, `CONNECTION_HELP_HTML`; a dropped
  connection is `GlueParent_OnEvent` `DISCONNECTED_FROM_SERVER` -> `GlueDialog_Show("DISCONNECTED")`. Character select:
  `CharSelectRealmName` (FontString), `CharSelectChangeRealmButton`, `CharSelectEnterWorldButton` (OnClick
  `CharacterSelect_EnterWorld`), `CharacterSelectBackButton` (OnClick `CharacterSelect_Exit`).
- **Interception:** the globals `GlueDialog_Show` and `GlueDialog_OnEvent` are wrapped (both are looked up by name when
  called). While the overlay is up, `CANCEL` dialogs are recorded and not shown; an `UPDATE` whose button text turns
  into something other than Cancel counts as an error. Error keys during the overlay, and `DISCONNECTED` at any time,
  become the friendly error. Everything else goes to the stock code unchanged.
- **Login state machine (open question 2):** left intact. Nothing calls into it except what the stock dialog's own
  buttons would: on an intercepted error the stock `OnShow` runs (e.g. `CONNECTION_HELP_HTML` calls
  `StatusDialogClick()` there), and Try again / Back to menu first run the stock Okay button's handler (`OnAccept` or
  `OnCancel`, whichever button reads Okay). Timeouts: the overlay offers "Back to menu" after 15 s, which runs the
  pending `CANCEL` dialog's own `OnAccept` (`StatusDialogClick`, the stock Cancel) and, on character select,
  `CharacterSelect_Exit`. Escape does the same once offered; other keys are ignored while the overlay is up (Enter
  would otherwise log in twice). Measured: login server down -> friendly error -> login server back -> Try again ->
  character select (`login.friendly_errors`).
- **Bug found and fixed:** `DefaultServerLogin` wipes its password argument in place (Measurements); `HSFrontMenu_Login`
  now keeps the password as bytes and builds a fresh string per login. Without this, Try again and Main Menu ->
  Continue fail with "Please enter your password." (the in-client-login front menu had the second bug already).
- **Overlay:** opaque black, the current backdrop's era logo, "Entering Azeroth..." in the glue font and a sweeping bar.
  It also covers the character-list refresh on character select (caption "Loading..."), e.g. after leaving the world.
- **Friendly error text** (`HS_FRIENDLY`): "Couldn't enter Azeroth" / "The world didn't answer this time. It usually
  works on the next try."; a disconnect: "You left Azeroth" / "The game stopped unexpectedly and brought you back to
  the menu." Buttons Try again, Back to menu; "Show details" shows the client's message as plain text.
- **Character select:** realm name and Change Realm hidden after the stock `CharacterSelect_OnShow` and again at each
  character list; Enter World reads Play, Back reads Main Menu. Their scripts are unchanged.
- **Test hook (HS_TEST only):** a compact record (token list in HSSeamless.lua's header) appended to the saved account
  list just before EnterWorld, which the client writes to WTF/Config.wtf. Texts are recorded as length + Adler-32
  (the 255-character cvar limit); the checks recompute them from the client's GlueStrings.lua and our sources. New test
  actions: details, retry, back, csinspect, play, showlog (debugging aid). The fifth marker square (second row) is green
  while the overlay is visible and blue while the friendly error is. Scripted actions now only repeat in the glue session
  that logged in (the glue screens restart at character select after the world).

### Wording add-on (patch 0003, `client-mods/addons/Moonglade/`)
- Delivered as loose files by a new data-patch kind `files` in `hs client-build` (only under `Interface/AddOns/`, only
  new paths, FrameXML/GlueXML refused, each output hashed into the stamp). Chosen over an MPQ because loose add-on
  folders are how the client finds non-Blizzard add-ons (`client/Interface/AddOns/` holds the Blizzard ones' folders);
  no binary or FrameXML change. Enabled by default (no AddOns.txt entry needed).
- Replaces, with the originals read from the client at load: LOGOUT, CAMP_TIMER (the countdown), CAMP_NOW,
  ERR_LOGOUT_FAILED, PLAYER_LOGOUT_FAILED, PLAYER_LOGOUT_FAILED_ERROR, IDLE_MESSAGE, ERR_COMSAT_DISCONNECT (FrameXML's
  only "lost connection" string), SERVER_MESSAGE_PREFIX ("[SERVER]" before server messages -> "[World]"). The game menu
  button and the CAMP popup, which copied the strings at load, are updated too. FrameXML has no world-disconnect string:
  that message is the glue's DISCONNECTED dialog, which the front menu replaces with the friendly error.
- Test hook: SavedVariables `MoongladeTest.hook = 1`, planted by the check into the scratch play client's
  `WTF/Account/HSTEST/SavedVariables/` only (the client reads SavedVariables from the logged-in account's folder; the
  account name isn't readable in the world, measured). Report at logout; the countdown popup is shown once and read.

### Server text (open question 3; `modules/mod-moonglade/`, `harness/hslib/config.py`)
- Sources found in the pinned core and modules: SMSG_MOTD (`CharacterHandler.cpp:843/:1173`; `MotdMgr::CreateWorldPacket`
  always appends a hard-coded AzerothCore line), the core version line (`Server.LoginInfo`, `CharacterHandler.cpp:846`,
  default 0, now pinned 0 in config.py), mod-playerbots' two login lines (`Playerbots.cpp:150-160`, sent whenever
  playerbots is enabled). Nothing else arrived in 30 s at login (probe).
- New module `mod-moonglade` (`Moonglade.QuietServerText`, default 1): a `ServerScript::CanPacketSend` hook drops
  SMSG_MOTD and the two playerbots system lines (matched by their exact opening text). A module, not a core patch, so
  no ADR. mod-playerbots' source note asks repacks/public servers to keep its line; Moonglade is personal and never
  distributed (CLAUDE.md section 1).
- Character logins are logged to a new timestamped server log, `env/logs/Players.log` (logger `entities.player`), for
  `login.charselect_wording`.

### Launcher (`harness/app/launcher.m`)
- Error dialogs: friendly main text (stage named from a friendly-name table: "Opening your save" instead of "Starting
  the database", "Getting ready to play" instead of "Starting the login server") and an NSAlert accessory view with a
  disclosure "Show details" that reveals the technical text (stage label, the play loop's error, the server's words)
  and the log path. Show Log stays. Hook event: `text` (main) and `details`.
- Backups window: "Restored. Your save is now from <date>." after a restore, "The restore didn't happen: <reason>."
  after a failed one (hook event `backups_status`).

### Checks
`login.loading_overlay`, `login.charselect_wording`, `client.wording_addon` share one ./play run; `login.friendly_errors`
has its own (it stops and restarts the login server); `client.login_text_clean` uses the probe on the running stack and
proves it can see such text by sending an `announce` (server message) and a `nameannounce` (system chat) to itself.
`app.startup_failure` now asserts the main text's wording and the details (error, server cause, log path, which it
used to assert on the single text); `app.backups_window` gained the done message and a damaged-backup run.

### F29
Patch 0002's title and the two mod-era-profile comment lines now say Moonglade; their three naming allowlist entries
are gone (the module change already changes the server build inputs).

### Verifier cycle 1 fixes (B26, 2026-10-07)
- **1.8 `client.wording_addon` flake, root cause:** a freshly created test character's first world login plays the
  intro cinematic (server: `CharacterHandler.cpp` "Show cinematic at the first time that player login"). During it
  `InCinematic()` is true, UIParent is hidden, and `StaticPopup_Show("CAMP")` returns nil (`StaticPopup.lua`: popups
  without `interruptCinematic` are refused), so the hook's timed popup read came out nil. Full runs recreate HSTEST
  (`fixture.account_character`, `play.player_account_untouched`), which is why only the full run hit it. Reproduced:
  `fixture.account_character` then the add-on run gave `CINEMATIC_START`, `show nil cin=1 uip=0`, popup nil; the next
  run (character no longer new) passed. Fix: the add-on's test hook is now sequenced by state, not timers. It skips the
  cinematic as the player's Escape does (`StopCinematic()`, repeated once a second while it runs, because a call during
  the loading screen doesn't take). It waits until the world UI has been up 3 s, shows the popup, reads it once its
  countdown is filled in, then logs out. It gives up after 20 s and says why in the report's `diag` timeline, which
  the check shows as `hook_timeline`. The check is unchanged in strength: it still requires the popup's text to match
  the new CAMP_TIMER format.
- **`files` patch kind:** sources must resolve to a file inside the repo. Add-on folders named `Blizzard_*` or already
  loose in the base client are refused. On a real build, a destination that any base archive holds (`mpq.find`) is
  refused too: a loose copy would shadow it.
- **1.9 per-check timing guard:** `harness/timing-baseline.json` (schema 2) holds each check's time minus its build
  time, with its source run (`harness/hslib/timingbase.py`). `runner.timing_summary` adds `build_s`,
  `build_by_check_s`, `new_checks_s` and `warnings`. `report.timing` compares the executed baseline checks' sum, each
  minus its build time (`build.clean` entirely; `core.login_logout_race_baseline`'s `build_s`, its baseline-server
  rebuild), with 110 % of their baseline sum. It also runs the spec's synthetic cases a–d through the same code on every
  run. summary.md gets a timing line plus one line per warning. At merge, new checks join with
  `harness/hs timing-baseline join <verifier results-full.json> --label <change>`, which only adds entries.
