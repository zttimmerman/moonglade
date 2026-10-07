# Design: seamless polish

## Overlay progress
- The client's login steps arrive as the stock status dialog texts: `OPEN_STATUS_DIALOG` / `UPDATE_STATUS_DIALOG`, with LOGIN_STATE_*, REALM_LIST_* and CHAR_LIST_* strings, which HSSeamless already intercepts.
- Map each known step to a target fraction, from the client's own GlueStrings (read with `hs mpq`, not from memory). Unknown texts advance by a small step.
- Each frame, the shown value eases toward a creep ceiling just below the next step's target, then jumps when that step arrives. `max()` keeps it from ever going down.
- `BeginPhase` resets to 0. `EndPhase("charlist")` sets it to full for the final frame before hiding.
- **Test hook:** the HS_TEST record has 255 characters at most. Per load, record a compact summary: the start value, the number of values, the number of decreases (must be 0), the number of step-driven advances, and the final value. Don't log every sample.

## Character list layout
- Measure the stock anchors in `CharacterSelect.xml`: the realm name, Change Realm, and the first `CharSelectCharacterButton`.
- When we hide the realm controls, re-anchor the first button (or the list container) upward by their measured offset.
- **Test hook:** record the first button's top and the panel's top (`GetTop()`) in the Q token, or a new compact token.

## As built
**Overlay bar** (`client-mods/glue/HSSeamless.lua`)
- `STEPS` lists nine GlueStrings keys in login order, with a mark for each: LOGIN_STATE_CONNECTING .10, HANDSHAKING .20, AUTHENTICATING .30, CHECKINGVERSIONS .35, AUTHENTICATED .45, REALM_LIST_IN_PROGRESS .55, REALM_LIST_SUCCESS .65, CHAR_LIST_RETRIEVING .75, CHAR_LIST_RETRIEVED .90.
  - The keys come from the installed client's `Interface\GlueXML\GlueStrings.lua` (patch-enUS-3.MPQ, read with `hs mpq extract`).
  - At run time each text is compared with the client's own global (`_G[key]`), so the wording is always the installed client's.
  - The marks are our choice (UI pacing, not game data).
- Every text the intercepted progress dialog reports goes through `HSSeamless_Step`, both from `GlueDialog_Show` (OPEN) and from `UPDATE_STATUS_DIALOG`.
  - A known text raises the reached mark to its own mark.
  - An unknown text moves the reached mark 30% of the way to the next mark.
  - The bar jumps to the reached mark at once.
- Each frame (`HSSeamless_OnUpdate`), the bar eases toward a ceiling 70% of the way from the reached mark to the next mark, using exponential approach at rate 1.2/s. It never reaches the next mark by time alone.
- `setBar` takes `max(current, new)`, so the bar never goes down.
- `BeginPhase` resets the bar to 0 (1 px drawn).
- `EndPhase("charlist")` sets it to full, then hides the overlay in the same call. The full bar is the overlay's last state rather than a separately drawn frame.
- **Test record:** `G:<first>:<samples>:<decreases>:<step advances>:<last>`, once per overlay, just before its `H`.
  - Values are per mille of the bar's drawn width, read back with `GetWidth()`: at BeginPhase, every frame, and at EndPhase.
  - A "step advance" means the drawn width grew inside a reported step's own event handler. No time passes there, so a bar that only moves with time scores 0. The triangle wave also scored 0 in counterexample A.
  - An earlier draft counted a ≥3% jump in the frame after a step. The triangle wave fooled it at low frame rates (it scored 2), so it was replaced.
- Main Menu on character select is recorded as `M`.
- Measured record for run B with the quick reload: 221 of 239 characters.

**Character list** (`HSSeamless_CharSelectWording`)
- Stock `CharacterSelect.xml` (patch-enUS-3.MPQ) lays out `CharacterSelectCharacterFrame` like this:
  - the realm name is anchored TOP y=-10 and is 13 high;
  - Change Realm is 33 high, directly below it;
  - `CharSelectCharacterButton1` is anchored TOPLEFT (24, -65).
  - That leaves a 9-unit gap below the realm controls.
- With the controls hidden, button 1 is re-anchored to TOPLEFT (24, -65+56) = (24, -9). Buttons 2 to 10 hang below it unchanged.
- **Test record:** `Y:<panel top>:<button 1 top>:<panel bottom>:<lowest bottom of buttons 1 to 10>`, rounded, recorded right after `Q` by `csinspect`.
  - Measured: `Y:753:744:111:161` (gap 9).
  - Without the re-anchor: `Y:753:688:111:105` (gap 65; button 10 sticks out 6 units below the panel).

**Harness** (`harness/hslib/checks/seamless_checks.py`; test actions in `HSFrontMenu.lua`)
- Run B (`play_session`, shared) now goes: Continue → character select (csinspect, shot) → `mainmenu` at +3 s → `continue` at +5 s → second character select → `charselect_again`: Play at +2 s.
  - New test actions: `mainmenu`, `continue` (in `HSFrontMenu_TestAct`), and the `HS_TEST.charselect_again` round.
  - The extra wall time is about 7 s; run B takes about 70 s in all.
  - The character-select screenshot is copied to `harness/out/seamless-charselect.jpg`.
- `login.overlay_progress` checks:
  - every STEPS key exists in the installed GlueStrings and the marks rise strictly;
  - the first load's G: first ≤ 5‰, ≥ 3 samples, 0 decreases, ≥ 1 step advance, last = 1000‰;
  - H is `charlist` and L ≥ 1.
- `login.overlay_progress_reload` checks:
  - the first load is complete and ends full;
  - `M` is followed directly by `C` (the Continue from the login screen);
  - the second load's G: first ≤ 5‰, 0 decreases, last = 1000‰, ending at character select.
- `login.charselect_layout` checks:
  - the stock gap (9), read from the installed client's CharacterSelect.xml;
  - Q is on charselect with the realm controls hidden, and the list holds ≥ 1 character;
  - |gap − 9| ≤ 2;
  - button 1's top ≤ the panel's top, and the lowest button bottom ≥ the panel's bottom.
- Existing checks are unchanged. login.loading_overlay, login.charselect_wording and client.wording_addon read the same record. Their assertions still hold with the extra M/C/O/G/H tokens, because each looks up the first `C`, the single `Q` and the first `P`.
