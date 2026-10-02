# Spec Delta: client-connectivity

## Purpose
Make the 3.3.5a (build 12340) Windows client, run under Wine (layer per ADR-0004), able to log in to the localhost server, and prove protocol-level connectivity headlessly.

## ADDED Requirements

### Requirement: Localhost realm
The realm list SHALL advertise exactly one realm, at 127.0.0.1:8085, accepting build 12340.

#### Scenario: Realmlist row is localhost
- **WHEN** the harness queries `auth.realmlist`
- **THEN** there is exactly one realm, address `127.0.0.1`, port 8085, and `gamebuild` 12340
- Check: `client.realmlist`

### Requirement: Headless protocol login probe
The harness SHALL include a headless client that speaks the 3.3.5a build 12340 protocol: SRP6 logon to authserver, realm list, world session auth, character enumeration, and character login into the world.

#### Scenario: Probe logs in and enters the world
- **GIVEN** the stack is up and the test account and character exist
- **WHEN** the probe connects to 127.0.0.1:3724 as build 12340 with the test account's credentials
- **THEN** logon proof succeeds, the realm list contains the localhost realm, world auth succeeds, character enum lists the test character, and after player login the server sends the login-verify-world packet with the character's map and position
- Check: `client.protocol_probe`

#### Scenario: Wrong password is rejected
- **WHEN** the probe tries to log on with a wrong password
- **THEN** the logon challenge succeeds (account exists) but the logon proof is rejected (AzerothCore reports a bad SRP6 proof as `WOW_FAIL_UNKNOWN_ACCOUNT` 0x04, AuthSession.cpp), no session key is issued, and no world session can be established
- Check: `client.protocol_badpass`

### Requirement: Client configuration
`harness/hs client-config <client-dir>` SHALL write `realmlist.wtf` (`set realmlist 127.0.0.1`) into the client's locale data folder(s) and set the relevant `WTF/Config.wtf` keys (realmlist, and patchlist pointed at localhost so no network patch server is contacted), preserving all other existing keys. It SHALL NOT modify any client binary.

#### Scenario: Config generation on a mock client tree
- **GIVEN** a temporary directory shaped like a 3.3.5a client (`Wow.exe` placeholder, `Data/enUS/`, `WTF/Config.wtf` with existing keys)
- **WHEN** `harness/hs client-config` runs against it
- **THEN** `Data/enUS/realmlist.wtf` contains `set realmlist 127.0.0.1`, `Config.wtf` has `SET realmlist "127.0.0.1"` and keeps its other keys, and the `Wow.exe` placeholder's hash is unchanged
- Check: `client.config_gen`

### Requirement: Human login gate
Zach SHALL be able to log in with the real client under Wine (ADR-0004) and walk around.

#### Scenario: Zach logs in and walks around
- **GIVEN** the stack is up and Zach's client is configured with `harness/hs client-config`
- **WHEN** Zach launches Wow.exe under the project's Wine layer and logs in with the account the harness printed
- **THEN** they reach character select, enter the world, and can move around
- [human-verify] Needs the licensed GUI client running under Wine, which the harness can't drive. This is the M0 human gate.

### Requirement: Client install and integrity
`harness/hs client-install <zip-or-dir>` SHALL install a 3.3.5a client into the repo's gitignored `client/` directory without modifying or deleting the source, and SHALL record a manifest (`env/client-manifest.json`: SHA-256 of every `.exe`/`.dll` and the MPQ file list) at install time. The harness SHALL then verify the installed client is build 12340 and hasn't been modified since install.

#### Scenario: Installed client is build 12340 and unmodified
- **GIVEN** a client installed with `harness/hs client-install`
- **WHEN** the harness inspects `client/`
- **THEN** `Wow.exe`'s PE version resource reads 3.3.5.12340, the client root contains no Direct3D/DXGI/DirectInput wrapper DLLs (`d3d8.dll`, `d3d9.dll`, `d3d10*.dll`, `d3d11.dll`, `dxgi.dll`, `dinput8.dll`, `dsound.dll`, `version.dll`, `winmm.dll`), and every `.exe`/`.dll` hash still matches the install manifest
- **AND** the install did not modify the source zip/dir (size and mtime recorded before and after the install are identical), and if the source still exists it still matches
- Check: `client.inspect`

### Requirement: Client launch under Wine
`harness/hs launch` SHALL bring the stack up (if needed), run `client-config`, ensure the test fixtures exist, and start `Wow.exe` under the project's Wine layer (ADR-0004) using a repo-local Wine prefix under `env/`, without modifying any client binary.

#### Scenario: Client launch smoke test
- **GIVEN** an installed client and the Wine layer from ADR-0004
- **WHEN** the harness runs `harness/hs launch --smoke`
- **THEN** a Wine `Wow.exe` process starts, is still running after 30 seconds without writing a crash report, and owns an on-screen window of at least 640×480 (per the macOS window server), and the Wine log shows no graphics errors during the run (Wine's `err:d3d`/`err:opengl`, or mtld3d's `ERROR mtld3d`); it is then terminated so that no Wine or `Wow.exe` process from this repo's prefix remains
- Note: whether the window shows the login screen can't be verified headlessly (screen capture is denied to the harness); the M0 human gate confirms it
- Check: `client.launch_smoke`
