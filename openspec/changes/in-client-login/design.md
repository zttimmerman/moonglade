# Design: In-client login screen

## Play client
- `env/client-play/` = `cp -cR client/ …` (APFS clone), then `patches/client/NNNN-*.json` (binary) and `patches/client/data/` (sources for patch MPQs) in order. A build stamp records the base manifest hash, each patch hash and each output hash; a rebuild with identical inputs is a no-op.
- `client.inspect` keeps guarding `client/`. The game writes `WTF/`, `Cache/` and `Logs/` into the play client, not into `client/`.
- One-time settings carry-over: if `client/WTF` holds player settings and the play client has none, copy them (never the other way).

## Patch 0001: GlueXML signature
- From the spike: `XML_VerifyTocSignature` @0x008165e0 returns 0/1/2/3, and the caller's switch quits on 0/1/2 (@0x004da7f1, @0x004da7fe, …). The builder pins the smallest edit that makes the result path behave as 3 (OK). Candidates: force the comparison/jump at the switch, or make the function return 3. Choose the one with the fewest bytes and no side effects, and document why. Record the before/after bytes and hashes.

## Login flow in GlueXML
- `AccountLogin.lua`/`.xml` overrides via our patch MPQ (`patch-enUS-4.MPQ` or the next free slot; the spike showed patch-N priority order).
- Credentials: the launcher writes a small generated Lua file (e.g. `Interface\GlueXML\HSProfiles.lua`, loose file or tiny MPQ) into the play client at each start. It holds the profile names and logins. It's the same secret already stored in `env/player.json` (gitignored); never committed, never written into `client/`.
- Continue: on show, the menu preselects the last-used profile's Continue button (Enter or click calls the client's login function). Other profiles are listed with Play. No countdown.
- Backdrop: `AccountLogin:SetModel(...)` plus glue music by choice. The choice is persisted in the client's own saved settings (or our generated file).
- Keep the realm list skipped (single local realm) and the character screen unchanged.

## Testing approach
- Runtime evidence without Screen Recording: auto-login is observable server-side (authserver/worldserver logs, account `last_login`, character enum).
- GlueXML errors land in `Logs/GlueXML.log` / `FrameXML.log` in the play client; the checks assert they're absent.
- Visual layout and backdrop look are human-verify.

## Risks
- The patched exe might behave differently in untested places → stock mode one setting away; human-verify play.
- The first launch of a new exe path under Wine may re-trigger macOS prompts → note for Zach.

## Menu layout (Zach + orchestrator UX review, 2026-10-04, after the first play test)
The first build worked but felt cluttered: Continue in the middle, a 3-button backdrop picker on the left, Options/Cinematics/Credits on the right. New layout, following single-player title screens:
- **Main menu**, one centred column in the lower-middle (backdrop art stays visible): **Continue** (preselected; current profile name in small text under it), **Profiles**, **Settings**, **Extras**, **Quit**.
- **Profiles** is always shown, even with one profile, so it's clear more can exist later. It lists profiles with a "current" mark; choosing one makes it Continue's target. Creating profiles is a later change.
- **Settings** submenu: **Game options** (opens the client's stock video/sound/interface option panels), **Login screen ◀ name ▶** (one cycling selector; backdrop, logo and music switch live), **Back**. World size and vanilla mode join here in the next change; nothing greyed out before it works.
- **Extras** submenu: **Cinematics**, **Credits**, **Back**.
- Keyboard first: Up/Down move, Enter selects, Escape goes back a level (Escape on the main menu does nothing or focuses Quit); mouse works too.
- A subtle dark translucent panel behind the menu column for legibility. Stock Blizzard button art and fonts.
- The logo stays top-left (don't cover the animation). Version, copyright and ESRB text go small at the bottom edge. No other buttons in the corners.
- Backlog: a setting to swap the WoW logo; creating profiles from the menu.

## Known limit: when choices are saved (verifier note, 2026-10-04)
The last profile and the backdrop choice are stored in the client's own settings (`WTF/Config.wtf`), which the client writes only when entering the world (EnterWorld → save routine; ADR-0009 appendix A). A backdrop picked and then quit from the menu without playing isn't remembered. `login.backdrop_choice` proves persistence through an enter-world step. Zach is told before his play session. A launcher-side store could lift this later, but the client has no reliable way to report the choice back.

## As built (builder B19, 2026-10-04): where the code differs from the sections above
- **Data patches** are records like the binary ones: `patches/client/0002-glue-front-menu.json` names the archive (`Data/<locale>/patch-<locale>-4.MPQ`) and its sources under `client-mods/glue/` (not `patches/client/data/`). `GlueXML.toc` is derived at build time from the client's own copy with `HSFrontMenu.xml` inserted before `GlueLocalizationPost.xml`, so no Blizzard file is committed. Build stamp: `<play client>/hs-build.json`. Revert: `hs client-build --skip 0001`; launcher setting `hs client-mode stock|patched`.
- **No stock GlueXML file is replaced.** `HSFrontMenu.xml/.lua` add the menu frames to `AccountLoginUI`, hide the stock controls and wrap `AccountLogin_OnShow` / `_OnKeyDown` / `_Login`.
- **Credentials file:** `Data/<locale>/patch-<locale>-5.MPQ` holding `Interface\GlueXML\HSProfiles.lua` (profiles with logins, the Continue context line, the About values; no other secrets), written by `harness/hslib/frontmenu.py` at each patched start, mode 0600, only into a play client (refused for `client/`). It is removed at cleanup; a stale copy, or a partial write (`.hs-new`) left by a killed run, is removed at the start of the next run.
- **Saved choices:** last profile = the stock saved account name (`accountName`); backdrop = the stock saved account list (`accountList`, `HSBD<key>|`), because the glue screens only know the client's registered settings. See "Known limit" above.
- **Menu as built** (after Zach's reviews of previews 2-4): under Continue a context line names the profile's last-played character ("Name · Level N Class", class from ChrClasses.dbc, computed by the launcher with a read-only query; profile name appended only with several profiles; "No characters yet"); it brightens with Continue and clicking it presses Continue. Extras has **About** (game version from the client, Hearthstone version and commit, core/playerbots pins, vanilla mode and world size of the running server, the stock copyright text). The bottom-edge text (version, copyright, Blizzard logo, ESRB notice) is hidden on the menu screens.
- **Testing as built:** runtime checks drive the real `./play` with the real Wow.exe on a scratch play client (`env/check-play-*`, deleted afterwards) with the harness test account only. Evidence: auth `last_login` newer than the client start and `online = 1` (worldserver session), the client's own `Logs/connection.log` (character list received), `Logs/GlueXML.log`/`FrameXML.log`, `WTF/Config.wtf`, and marker squares the client draws in test mode (backdrop, music, main menu up, Lua-error count from a test-only `seterrorhandler`) read back from its own `Screenshot()` files. The test hook (`$HS_GLUE_TEST`) and `$HS_PLAY_SKIP_PATCHES` are refused by a normal `./play` and never apply to the player's account.
- **Passwords:** in app runs (`HS_FROM_APP`, set by the launcher) `./play` prints where the password is kept instead of the password; harness results are redacted before they're written.
