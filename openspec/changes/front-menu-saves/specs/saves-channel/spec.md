## ADDED Requirements

### Requirement: The load verb
The control listener SHALL accept the verb `L` with the argument `<generation hex digit><two-digit index>`. It SHALL map it to a save through the list most recently handed to the glue, ask the server to load that save, and reply ACCEPTED only after the server reports the load done. It SHALL reply REFUSED for a stale generation, an unknown index, a refused or failed load, or a second load while one is running. It SHALL never put a save id or path in a token, a log or `play-last.json`.

#### Scenario: Load verb, offline
- **GIVEN** a listener with a stub load action that records its arguments and can be told to succeed, refuse or stall
- **WHEN** challenges arrive for a valid `L`, a stale generation, an unknown index, a refused load, a load that never finishes within the poll limit, and two overlapping loads
- **THEN**
  - only the valid one that succeeds gets ACCEPTED, and the others get REFUSED;
  - the stub saw exactly one call per non-stale request;
  - the log names the verb and outcome but no save id
- Check: `savesctl.load_verb`

### Requirement: Only the 3.3.5a client is served
The listener SHALL refuse a challenge whose build is not 12340 or whose version is not 3.3.5, even with the right nonce.

#### Scenario: Wrong build refused
- **GIVEN** a listener with a known nonce
- **WHEN** valid `S` challenges arrive with builds 12340, 12339 and 8606, and with version 2.4.3
- **THEN** only build 12340 at 3.3.5 gets ACCEPTED. The others get REFUSED and are logged with the reason "wrong build"
- Check: `savesctl.build_refused`

### Requirement: Control requests don't change the saved profile
A control request SHALL leave the client's saved account name (`accountName` in `WTF/Config.wtf`) as it was before the request. At each start, the launcher SHALL replace a saved account name that looks like a control token.

#### Scenario: accountName survives control requests
- **GIVEN** a test run on the title menu, with `accountName` set to the test profile's account
- **WHEN** the script sends two `ctl:S` requests, then continues, enters the world and quits
- **THEN** `WTF/Config.wtf` afterwards has `accountName` equal to the test account, and no `HS…` token appears anywhere in `WTF/`
- Check: `savesctl.account_name_kept`

#### Scenario: A leftover token is healed at start
- **GIVEN** a scratch play client whose `Config.wtf` has `accountName` set to an `HS<6 hex>S` token
- **WHEN** `./play` (test mode) starts and writes its client config
- **THEN** `accountName` is the default profile's account before the client starts
- Check: `savesctl.account_name_kept`
