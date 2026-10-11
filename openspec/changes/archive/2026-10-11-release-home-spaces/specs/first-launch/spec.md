## ADDED Requirements

### Requirement: A release HOME path with spaces works
First launch and every later start SHALL work when the Moonglade folder's path contains spaces, as the real one (`~/Library/Application Support/Moonglade`) does. The servers SHALL never receive a configuration path they reject.

#### Scenario: Fresh HOME with a space to ready
- **GIVEN** the "Fresh HOME to ready" setup with a scratch HOME whose path contains a space
- **WHEN** the app's first launch runs
- **THEN** it reaches ready, the login and world servers started from the HOME's own configs, and the evidence shows the HOME path with its space
- Check: `firstlaunch.flow`

#### Scenario: The servers start from a spaced HOME
- **GIVEN** a scratch HOME whose path contains a space, set up from the payload
- **WHEN** the stack starts from the payload
- **THEN** mysqld, authserver and worldserver come up and stop cleanly, and neither server's stderr says its `--config` argument is invalid
- Check: `release.stack_from_payload`
