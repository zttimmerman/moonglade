# Spec Delta: login-screen

## ADDED Requirements

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
