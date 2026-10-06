# Spec Delta: play-launcher

## ADDED Requirements

### Requirement: Harness runs never change the player's play client
No harness run SHALL write, launch the real game in, or otherwise change the player's own play client (`env/client-play/`) settings. Only a genuine player launch (the app, or plain `./play` with no harness signs) may apply client config, resolution and the login file there.

#### Scenario: A harness run leaves the player's Config.wtf byte-identical
- **GIVEN** the player's play client with its current `WTF/Config.wtf`
- **WHEN** any harness run (quick, full, gui or slow tier) completes
- **THEN** that file's hash at the end equals its hash at the start of the run, and no launcher marker file was created
- Check: `client.player_config_untouched`

#### Scenario: Config writes are gated on the player's own client
- **GIVEN** a scratch play client marked as the player's own (test switch), lacking a Wine default such as gxApi
- **WHEN** a harness-shaped `./play` stand-in run uses it, and separately an ungated config step is applied to a copy
- **THEN** the gated run changes no config file and writes no login file, while the ungated control adds the missing default (proving the guard bites)
- Check: `client.config_gate`

#### Scenario: Launch-shape gate decisions
- **GIVEN** the launch shapes the harness and the player use (app, plain ./play, ./play with test arguments or harness environment switches, scratch vs own client)
- **WHEN** the gate decides whether a launch may write the play client's config
- **THEN** only genuine player launches on the player's own client (and scratch clients) get writes; every harness-shaped launch on the player's own client gets none
- Check: `client.resolution_gate`

#### Scenario: No real game in the player's client from the harness
- **GIVEN** a harness-shaped `./play` with no stand-in client, targeting the player's own play client
- **WHEN** it starts
- **THEN** it is refused before the server starts, nothing is launched, and nothing is changed
- Check: `client.real_client_refused`
