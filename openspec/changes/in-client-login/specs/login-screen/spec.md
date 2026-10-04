# Spec Delta: login-screen

## Purpose
The client's own login screen as the single-player front menu: pick a profile and play, no typing.

## ADDED Requirements

### Requirement: Auto-login with the last profile
On start, the login screen SHALL log in with the last-used profile automatically after a short visible countdown, unless the player cancels.

#### Scenario: Launch lands in character select without typing
- **GIVEN** the play client with our GlueXML and a profile (the PLAYER account, or a scratch test profile)
- **WHEN** the real client is launched by the play loop and no input is sent
- **THEN** within 90 s the authserver records a successful login for that profile's account and the worldserver serves its character list, with no keystrokes injected by the harness
- Check: `login.auto_login`

#### Scenario: Cancel shows the profile list
- **GIVEN** the same setup with the cancel option triggered (the documented key/button, or a test flag in the generated profile file that simulates it)
- **WHEN** the client starts
- **THEN** no login is attempted within the countdown window plus 30 s, and the profile list is shown (evidence: no authserver login attempt; GlueXML state marker if available)
- Check: `login.cancel_shows_list`

#### Scenario: No UI errors
- **WHEN** the patched client starts with our GlueXML (auto-login and cancel runs)
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
- **WHEN** Zach opens it, lets it auto-login, then restarts, cancels, and switches the backdrop
- **THEN** he reaches character select without typing, and the screen looks and sounds like a native login screen
- [human-verify] Look, feel and sound of the login screen can only be judged by a person.
