## Design
Follow the spike doc's "A1 in detail" and "Live test plan for A1" sections (`git show origin/spike/front-menu-saves:docs/research/front-menu-saves-spike.md`).
- **Listener.** A thread in the launcher process (`launch.py` / app): bind 127.0.0.1 on port 0, then pass the port and nonce to the glue the way other launch data is passed (HSProfiles.lua generation), never inside `client/`. Read a single challenge per connection, capped at 64 KiB. Fixed reply codes, picked from the live table. A run-scoped log line names only the verb and the outcome.
- **Glue.** Put the frames in `HSFrontMenu.xml`, since stock GlueXML may not expose `CreateFrame`. Hook the status-dialog updates while a request is pending, call `StatusDialogClick()` to dismiss, and reset `realmList` on reply or after a 5 s timeout.
- **Test actions.** Add `ctl:<verb>` and `realm:reset` to the HS_GLUE_TEST action set. They are refused through the existing test-switch guard.
- **Tiers.** The offline checks (`listener`, `test_actions_refused`) must cost under 1 s, because the quick tier has about 6 s of headroom. Put them in full if they can't fit. The live checks are GUI, full tier (or slow if long). Share one client session across the live scenarios where possible.

## As built
- **Listener** (`harness/hslib/savesctl.py`). `Listener` binds 127.0.0.1 on port 0 and serves on a daemon thread. Each launch gets a fresh 6-hex-digit nonce (`secrets`). Token: `HS<nonce><verb><arg>`; only `S` (no argument) exists. It reads one challenge per connection: at most 64 KiB read, a declared body of at most 1 KiB (a real challenge is 30 bytes plus a name of up to 255), and a 2 s read timeout. A truncated or oversized packet is closed with no reply. A wrong nonce, unknown verb, unexpected argument, malformed challenge or non-control name gets REFUSED. A valid request gets ACCEPTED. A success code can't be sent: `_reply` raises on 0, and the test knobs reject 0.
- **Linger (found live).** If the listener closed right after the 3 reply bytes, the client showed "You have been disconnected from the server." (`DISCONNECTED`) for every code, not the code's own text. Now the listener waits for the client to close first, up to `LINGER_S` = 2 s. The real authserver behaves the same way: it sends the failure and keeps reading.
- **Log.** `env/logs/savesctl.log` is rewritten at each start, with one JSON line per connection: outcome, reason, verb, source address, build and reply code. The same events go into `play-last.json` under `savesctl`. Neither ever holds the nonce or the account name.
- **Wiring** (`play.py`). The listener starts when the play loop writes the generated login file (patched mode, the same gate). Its port and nonce go into `HSProfiles.lua` as `HS_CONTROL` (`frontmenu.render(control=)`). That file is 0600, inside the play client only, and removed on exit. The evidence `frontmenu.write` returns names only the port. The listener stops first thing in `Loop.cleanup`. After a SIGKILL the socket dies with the process. The next run removes the stale login file before anything else (existing `select_client`) and makes a new nonce. The app runs `./play`, so it gets the same wiring.
- **Test knobs.** `$HS_SAVESCTL_TEST` (`codes`, `delay_s`, `stop_after`) is refused by `./play` without `--allow-test-client-dir`. With the flag it is still refused unless `--account test` is given. It is listed in `HARNESS_ENV`.
- **Glue** (`HSFrontMenu.lua`, the "saves-channel" section; frames `HSControlOverlay` and `HSControlFrame` in `HSFrontMenu.xml`). No new files and no change to the patch 0002 manifest.
  - `HSControl_Send(verb, arg)` works only on the login screen, with `HS_CONTROL` present, nothing pending and no loading overlay up. It saves `realmList`, sets it to `127.0.0.1:<port>` and shows the "One moment…" overlay. It then calls `DefaultServerLogin(token, <throwaway password per request>)`. A fresh password string is needed because `DefaultServerLogin` wipes it in place.
  - While a request is pending, and for 2 s after it, `GlueDialog_OnEvent` and `GlueDialog_Show` (wrapped inside HSSeamless's wrappers) swallow `OPEN`/`UPDATE`/`CLOSE_STATUS_DIALOG`. The first non-`CANCEL` dialog text is the reply.
  - On the next frame the glue calls `StatusDialogClick()` (the stock Okay), restores `realmList`, hides the overlay and classifies the reply. The text of `HS_CONTROL_REPLY.accepted` (`LOGIN_DBBUSY`) gives A, `refused` (`LOGIN_BANNED`) gives R, and anything else gives U ("couldn't reach Moonglade"). With no reply after `HS_CONTROL_TIMEOUT` = 5 s, the glue calls `StatusDialogClick()` (the stock Cancel) and records U.
- **Test hook.** Two new actions: `ctl:<verb><arg>` and `realm:reset`. Each result is a record token `K<A|R|U><realm restored 0/1><stock dialog seen 0/1>:<Adler-32 of the text>:<tenths of a second>`, handed out through `accountList` at EnterWorld (HSSeamless's existing record). Two new marker squares sit in the second row: control state (magenta while pending, then green/red/blue for A/R/U, white before any request) and stock dialog visible (red/white).
- **Checks.**
  - `savesctl.listener` is quick tier (about 0.8 s, in-process loopback).
  - `savesctl.test_actions_refused` is full tier: five `./play` refusal runs plus a static and in-process check.
  - `savesctl.hygiene` is full tier: stand-in client, two runs with a SIGKILL between them.
  - The four live checks are full tier and share one real-client run (`live_session`) that takes about 3 minutes.
- **Authserver log clause.** At our log level (`Logger.root=4`, info), AzerothCore doesn't log challenges (`[AuthChallenge]` is DEBUG). An absent HS name in `Auth.log` is therefore weak evidence on its own. `live_roundtrip` adds the client's own `connection.log`: every connection before the Continue went to `127.0.0.1:<listener port>`, and the first one to 3724 is the Continue's.

## Live results (2026-10-09; appended to the spike's facts, not the spike branch)
A1 works live on the real client (`Wow.exe` 3.3.5.12340 via the ADR-0009 play client, under Wine). The run used a scratch play client and the test account HSTEST.
1. **`realmList` can be set from glue, and `:port` is honoured.** The listener got the challenge with account `HS<nonce>S`, build 12340, version 3.3.5, from 127.0.0.1. The client's `connection.log` shows `RESPONSE_CONNECTED … 127.0.0.1:<listener port>`.
2. **The reply is readable.** It reaches the glue as `OPEN_STATUS_DIALOG("OKAY", <text>)` after `CANCEL` progress dialogs ("", "Connected", "Authenticating"). This works only once the listener lets the client close first (see Linger).
3. **Reply-code table.** The texts were matched by Adler-32 against the installed client's GlueStrings.lua, and match the client's own `connection.log` result names.

| code | glue text (GlueStrings key) | unique among tested | use |
|---|---|---|---|
| 0x03 | LOGIN_BANNED | yes | **refused** |
| 0x04 | LOGIN_UNKNOWN_ACCOUNT (same text as LOGIN_MALFORMED_ACCOUNT_NAME) | yes | |
| 0x07 | LOGIN_NOTIME | yes | |
| 0x08 | LOGIN_DBBUSY | yes | **accepted** |
| 0x0C | LOGIN_SUSPENDED | yes | |
| 0x10 | LOGIN_LOCKED_ENFORCED (same text as AUTH_LOCKED_ENFORCED) | yes | |

4. **Timings.**
   - With the test's 1.5 s reply delay, send to result took 1.5–1.7 s, so the round trip itself is about 0.1–0.2 s.
   - With the listener down, the client reports `LOGIN_FAILED` ("Unable to connect", via `CONNECTION_HELP_HTML`) in under 0.1 s, so the 5 s timeout isn't what catches it.
5. **Restoring `realmList` and the stock dialogs.**
   - `realmList` was restored after every request.
   - `WTF/Config.wtf`, written at EnterWorld, has `realmList "127.0.0.1"`.
   - The Continue afterwards logged in at 127.0.0.1:3724 and reached the character list.
   - No stock dialog was on screen in any frame of any request. The screenshot taken while a request was pending shows our overlay.
