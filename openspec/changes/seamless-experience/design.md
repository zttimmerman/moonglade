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
