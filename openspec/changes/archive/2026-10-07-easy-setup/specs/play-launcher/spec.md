# Spec Delta: play-launcher

## ADDED Requirements

### Requirement: Harness runs never rebuild the player's play client
No harness run SHALL rebuild, re-patch or replace any file of the player's own play client (`env/client-play/`). Only a genuine player launch (the app, or plain `./play` with no harness signs) may rebuild it when its patch inputs changed.

#### Scenario: A full run leaves the play client's build untouched
- **GIVEN** the player's play client, with its `hs-build.json`, `Wow.exe` and patch outputs fingerprinted, and patch inputs that differ from the ones it was built with (e.g. a changed `client-mods/` file, in a scratch branch)
- **WHEN** a full-tier harness run completes
- **THEN** the fingerprint is byte-identical at the end of the run; and a genuine-launch-shaped control (on a scratch copy marked as the player's) does rebuild it, which proves the guard bites
- Check: `client.player_client_not_rebuilt`
