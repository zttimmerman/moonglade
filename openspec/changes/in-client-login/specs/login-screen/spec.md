# Spec Delta: login-screen

## Purpose
The client's own login screen as the single-player front menu: pick a profile and play, no typing.

## ADDED Requirements

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
