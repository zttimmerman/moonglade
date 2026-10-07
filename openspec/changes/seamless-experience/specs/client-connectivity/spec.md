# Spec Delta: client-connectivity

## ADDED Requirements

### Requirement: No server-identity text on login
Entering the world SHALL NOT produce a message of the day or any server or module announcement that names a server, realm, core or bot module.

#### Scenario: Login chat is clean
- **GIVEN** a running stack with the default world
- **WHEN** a character logs in through the headless protocol probe and stays in the world for 30 s
- **THEN** the probe receives no SMSG_MOTD lines and no system or chat message containing "server", "realm", "AzerothCore", "playerbot" or "mod-" (case-insensitive) addressed to that character; and the check shows it can see such a message by enabling one (e.g. a test MOTD) and observing it
- Check: `client.login_text_clean`

### Requirement: In-game wording add-on
The play client SHALL include a Moonglade add-on, enabled by default for the player's account, that replaces server-flavoured interface strings at runtime with game wording, at least: Logout → "Exit to Menu" (and its countdown text), and the in-world idle-disconnect and lost-connection messages that exist in the client's interface strings (IDLE_MESSAGE, ERR_COMSAT_DISCONNECT). Being disconnected from the world returns the player to the front menu, where the message is the login screen's friendly error ("Friendly errors on the front menu"). It SHALL NOT modify FrameXML files and SHALL load with no Lua errors.

#### Scenario: Add-on loads and renames
- **GIVEN** a scratch patched play client
- **WHEN** a test character enters the world and logs out
- **THEN** the add-on is listed as loaded and enabled; its test output (SavedVariables written at logout) shows each listed global string with its new value and the original value read from the client; the error count is 0; and no file under `Interface/FrameXML` differs from the stock client's
- Check: `client.wording_addon`
