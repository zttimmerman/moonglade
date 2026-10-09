## Why
Saves in the front menu need the title menu (the glue login screen) to ask the launcher to do something: "load save 3" or "what's the status?". The spike (`docs/research/front-menu-saves-spike.md` on `spike/front-menu-saves`, B27, 2026-10-06) recommends **channel A1**, at about 85% confidence. The menu briefly points its login at a 127.0.0.1 listener the launcher opens and "logs in" with a coded account name. The listener answers with a chosen failure code, and the menu reads that code as the reply.

The game half of A1 has never run live. A live test attempted on 2026-10-09 couldn't run without new glue code, so this change builds that glue code and the listener and proves the channel live. The channel doesn't depend on any of Zach's pending saves decisions (slots, quickload, ironman). The `front-menu-saves` change builds on it.

## What Changes
- **Launcher control listener** (`harness/hslib/savesctl.py`). It runs only while the game runs, binds to 127.0.0.1 only, and uses a per-launch nonce.
  - It reads exactly one AUTH_LOGON_CHALLENGE per connection, with a size cap, and never sends a success.
  - The token format is `HS<nonce><verb><arg>`. This change implements only the `S` (status) verb; `R` (restore) comes with `front-menu-saves`.
- **Glue sender** (`client-mods/glue`).
  - It sets `realmList` to the listener, calls `DefaultServerLogin(token, "X")`, and reads the status-dialog text.
  - It hides the stock "Connecting" and error dialogs while a control request is pending.
  - It resets `realmList` when the reply arrives or after a timeout.
- **Test actions** `ctl:<verb>` and `realm:reset` for `HS_GLUE_TEST` scripts. They are refused outside test runs.
- **Live checks** pin the reply-code → text table and show that the menu returns to a normal login afterwards.

## Fallback
If the live test shows A1 can't work, the builder stops and reports with evidence. The ways it could fail: `realmList` can't be set from glue, the port is ignored, or the reply can't be read. In that case the change moves to the spike's fallback C1, the screenshot code. That is recorded here as a spec change, and Zach is not asked.

## Sources
- Spike B27's static RE of `Wow.exe` 3.3.5.12340: call-time read of `realmList`, `:port` parsing, the status-dialog flow.
- Live-test attempt notes, 2026-10-09: in enUS, `LOGIN_UNKNOWN_ACCOUNT` and `LOGIN_MALFORMED_ACCOUNT_NAME` have the same text. So "accepted" and "refused" must use codes whose texts are unique; the live check pins the table.

## Spec-change log
- 2026-10-09 (verifier note, clarification): "Status round trip" THEN. At our log level AzerothCore never logs logon challenges (`[AuthChallenge]` is DEBUG, `AuthSession.cpp:293`), so the original clause "the authserver's log shows no login attempt with an `HS` name" could never fail. The clause now also requires the client's `connection.log` to show that every control request went to the listener's port, and that the only 3724 connection is the later Continue. The Auth.log assertion is kept, and the check already asserted both. This is a tightening of what is proven, not a loosening.

## Follow-ups (from the verifier)
- Check whether the client stores the last account name (`HS<nonce>S`) in `WTF/Config.wtf` (`accountName`). If it does, `front-menu-saves` must restore or clear it.
- Decide whether the listener should refuse builds other than 12340. Today a valid token with any build is accepted; that's harmless, since it's loopback-only and needs the nonce.
