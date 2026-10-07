# login-screen Specification

## Purpose
The client's own login screen as the single-player front menu: pick a profile and play, no typing.

## Requirements

### Requirement: One-press Continue from the menu
On start, the login screen SHALL show the menu (no username or password fields) with a preselected **Continue** button for the last-used profile; Enter or a click logs in with no typing. There is no countdown or automatic login.

#### Scenario: Continue lands in character select without typing
- **GIVEN** the play client with our GlueXML and a profile (a scratch test profile, never PLAYER in checks)
- **WHEN** the real client is launched by the play loop and a single Continue action is triggered (a test flag in the generated profile file standing in for Enter/click; no keystrokes injected)
- **THEN** within 90 s the authserver records a successful login for that profile's account and the worldserver serves its character list, with no credentials typed
- Check: `login.auto_login`

#### Scenario: Menu waits for the player
- **GIVEN** the same setup with no Continue action
- **WHEN** the client starts
- **THEN** no login is attempted for at least 60 s after the menu appears, and the menu (profile list with Continue) stays up (evidence: no authserver login attempt; GlueXML state marker if available)
- Check: `login.cancel_shows_list`

#### Scenario: Continue shows where you left off
- **GIVEN** a profile whose account has characters, and another with none
- **WHEN** the launcher generates the front-menu data at start
- **THEN** the line under Continue names that account's most recently played character with its level and class (class name taken from the game's own data, e.g. ChrClasses.dbc, not a hardcoded table), the profile name is appended only when more than one profile exists, and the profile with no characters shows "No characters yet"; no credentials appear in the line or its data
- Check: `login.continue_context`

#### Scenario: No UI errors
- **WHEN** the patched client starts with our GlueXML (Continue and menu-waits runs)
- **THEN** the play client's `Logs/GlueXML.log` and `Logs/FrameXML.log` contain no errors from our files, and the client doesn't quit by itself
- Check: `login.no_glue_errors`

### Requirement: Backdrop picker
The login screen SHALL offer WotLK, TBC and Vanilla (Dark Portal) backdrops with their matching music, and remember the choice.

#### Scenario: Backdrop choice is applied and remembered
- **GIVEN** a stored backdrop choice of TBC
- **WHEN** the client starts
- **THEN** the TBC backdrop model and music are the ones requested by GlueXML (evidence from the client's own state/log, or the generated settings), and the choice persists across a restart
- Check: `login.backdrop_choice`

#### Scenario: Zach logs straight in and tries the picker
- **GIVEN** the .app with the play client
- **WHEN** Zach opens it, presses Continue, then restarts and switches the backdrop
- **THEN** he reaches character select with one press and no typing, and the screen looks and sounds like a native login screen
- [human-verify] Look, feel and sound of the login screen can only be judged by a person.

### Requirement: A fresh play client waits for the player
A never-used play client SHALL NOT log in before the player triggers Continue.

#### Scenario: No login before Continue on first launch
- **GIVEN** a freshly built, never-used scratch play client
- **WHEN** the client starts and the menu appears
- **THEN** no authserver login happens between client start and the Continue action
- Check: `login.fresh_menu_waits`

### Requirement: Loading overlay instead of connection dialogs
After Continue, the front menu SHALL show a themed "Entering Azeroth…" overlay until character select appears, and none of the stock connection dialogs (connecting, authenticating, success, retrieving the character list, handshaking) SHALL become visible in that time.

#### Scenario: Continue shows only the overlay
- **GIVEN** a scratch patched play client and a running stack
- **WHEN** Continue is pressed and the client reaches character select
- **THEN** the overlay is shown from the Continue press until character select; the client records no visible stock connection dialog in that time (each attempt to show one is intercepted and logged by the test hook, and the check asserts the hook ran); there are no Lua errors; and a screenshot taken while connecting shows the overlay
- Check: `login.loading_overlay`

### Requirement: Character select without server wording
Character select SHALL NOT show the realm name or the Change Realm button, and its buttons SHALL read **Play** (Enter World) and **Main Menu** (Back). The new labels SHALL perform the same actions as before.

#### Scenario: Character select wording
- **GIVEN** a scratch patched play client at character select with one character
- **WHEN** the screen is inspected through the test hook, then Play is activated
- **THEN** the realm name and Change Realm are hidden; the two buttons read "Play" and "Main Menu"; activating Play enters the world (server log shows the character login)
- Check: `login.charselect_wording`

### Requirement: Friendly errors on the front menu
A login or connection failure SHALL show a plain message (no protocol or server wording) with **Try again** and **Back to menu**, and a **Show details** control that reveals the client's original error text. Try again SHALL retry the login.

#### Scenario: Login server down, then back
- **GIVEN** a scratch patched play client and a stack whose login server is stopped
- **WHEN** Continue is pressed, the friendly error appears, details are opened, the login server is started, and Try again is pressed
- **THEN** the friendly message contains none of the words server, realm, connect, authenticat or account; the details show the stock error text that the client would have shown; Try again reaches character select
- Check: `login.friendly_errors`

#### Scenario: Zach sees no server wording
- **WHEN** Zach starts Moonglade, presses Continue, picks a character, plays, and exits to the menu
- **THEN** nothing he sees says server, realm, connecting or account
- [human-verify] Feel and wording across the whole flow are judged by a person.

### Requirement: The loading overlay shows real, forward-only progress
While the "Entering Azeroth…" overlay is up, its bar SHALL reflect the login's real progress:
- it advances when the client reports the next login step;
- it never decreases during one load;
- it is full when the character list appears;
- it starts from empty at the beginning of every load, including a load right after returning to the main menu.

#### Scenario: Progress only moves forward and ends full
- GIVEN a scratch patched play client at the front menu
- WHEN the player presses Continue and the login reaches character select
- THEN every recorded bar value during that load is greater than or equal to the one before it
- AND the bar advanced at least once in response to a client-reported login step (not only with time)
- AND the last value before the overlay hides is full
- Check: `login.overlay_progress`

#### Scenario: A quick second load starts from zero and never goes backwards
- GIVEN the player has just reached character select through the overlay
- WHEN the player clicks Main Menu and presses Continue again within a few seconds
- THEN the second load's first recorded bar value is empty, at or below the start value
- AND the second load's values never decrease and end full
- Check: `login.overlay_progress_reload`

### Requirement: The character list starts at the top of its panel
With the realm name and Change Realm hidden, the first character in the character select list SHALL sit at the top of the list's panel. The gap between the panel's top edge and the first character is no larger than stock WoW's gap when the realm controls are shown, minus their height.

#### Scenario: No empty gap above the first character
- GIVEN character select on a patched play client with at least one character
- WHEN the screen is shown
- THEN the vertical gap between the character list panel's top edge and the first character button's top edge is within a small tolerance of the stock layout's gap below the realm controls
- AND the realm name and Change Realm stay hidden, and every character button stays inside the panel
- Check: `login.charselect_layout`
