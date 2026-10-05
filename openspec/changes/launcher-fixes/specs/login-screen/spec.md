# Spec Delta: login-screen

## ADDED Requirements

### Requirement: A fresh play client waits for the player
A never-used play client SHALL NOT log in before the player triggers Continue.

#### Scenario: No login before Continue on first launch
- **GIVEN** a freshly built, never-used scratch play client
- **WHEN** the client starts and the menu appears
- **THEN** no authserver login happens between client start and the Continue action
- Check: `login.fresh_menu_waits`
