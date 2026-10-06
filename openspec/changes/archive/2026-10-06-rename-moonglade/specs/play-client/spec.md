# Spec Delta: play-client

## MODIFIED Requirements

### Requirement: Stock mode one setting away
With `client = stock`, `./play` and the .app SHALL launch the pristine `client/` exactly as before this change.

#### Scenario: Stock mode launches the untouched client
- **GIVEN** `client = stock` in a scratch settings file
- **WHEN** the play loop runs with the stand-in check client path replaced by the real Wow.exe launch smoke
- **THEN** the launched executable is `client/Wow.exe` (pristine hash), and with `client = patched` it's a play client's `Wow.exe` built by the same patch queue (patched hash): `env/client-play/` for the player, a scratch play client in harness checks
- Check: `client.stock_mode`
