## ADDED Requirements

### Requirement: Launcher control listener
While the game runs from `./play` or the app, the launcher SHALL open a control listener on 127.0.0.1 only, with a per-launch random nonce. The listener SHALL accept requests only as the account name of an AUTH_LOGON_CHALLENGE (`HS<nonce><verb><arg>`), SHALL answer every challenge with a failure code (never a success and never an SRP proof), and SHALL close when the game exits.

#### Scenario: A status request with the right nonce is accepted
- **GIVEN** a listener started with a known nonce (offline, no client)
- **WHEN** a recorded 3.3.5a (build 12340) logon challenge with account name `HS<nonce>S` arrives
- **THEN** the listener replies with the "accepted" code (3 bytes, `00 00 <code>`), logs the verb and the source address, sends nothing else, and closes the connection. No reply anywhere in the run is a success code
- Check: `savesctl.listener`

#### Scenario: Wrong nonce, malformed or oversized requests are refused
- **GIVEN** the same listener
- **WHEN** challenges arrive with a wrong nonce, an unknown verb, a truncated packet, and a packet over the size cap
- **THEN** each gets the "refused" code, or a close with no reply for the truncated and oversized packets; each is logged as refused; and no action runs
- Check: `savesctl.listener`

#### Scenario: Loopback only, secret handled safely
- **GIVEN** a `./play` run in test mode
- **WHEN** the listener is up
- **THEN**
  - it is bound to 127.0.0.1 and not to any other address (lsof);
  - the file handing the port and nonce to the glue is not in `client/`;
  - the nonce appears in no harness log, `results.json` or `summary.md`;
  - the listener is gone after the game exits, and after a SIGKILL of `./play`, the next run starts cleanly with a fresh nonce
- Check: `savesctl.hygiene`

### Requirement: The menu sends a request and reads the reply
From the title menu, the glue SHALL send a control request by pointing `realmList` at the listener and logging in with the token. It SHALL read the reply from the status-dialog text, SHALL hide the stock connecting and error dialogs while the request is pending, and SHALL restore `realmList` afterwards. The real login server SHALL never see a control token.

#### Scenario: Status round trip from the menu
- **GIVEN** the play client on the title menu in a test run, with the listener up
- **WHEN** the test script sends `ctl:S`
- **THEN**
  - the listener logs exactly one challenge with account `HS<nonce>S`, build 12340, from 127.0.0.1, within 5 s;
  - the glue records the "accepted" reply;
  - a screenshot during the request shows our overlay, not the stock Connecting or error dialog;
  - no control-request connection reaches the login server's port (the client's `connection.log` shows each one going to the listener's port, and the only 3724 connection is the later Continue), and the authserver's log names no `HS` token
- Check: `savesctl.live_roundtrip`

#### Scenario: The reply-code table is pinned
- **GIVEN** the same setup
- **WHEN** the listener answers successive `ctl:S` requests with each candidate code (at least 0x03, 0x04, 0x07, 0x08, 0x0C, 0x10)
- **THEN** the glue records each code's status text. The "accepted" and "refused" codes the design uses each map to a text that no other tested code produces, and the table is written to the check's evidence
- Check: `savesctl.live_codes`

#### Scenario: Listener down
- **GIVEN** the title menu in a test run with the listener stopped
- **WHEN** the script sends `ctl:S`
- **THEN** within 10 s the glue records "couldn't reach the launcher", restores `realmList`, and nothing else changes
- Check: `savesctl.live_down`

#### Scenario: Normal login afterwards
- **GIVEN** a test run that has just completed a control request
- **WHEN** the script continues to log in and enter the world, then quits
- **THEN** login reaches the real realm and the test character enters the world, and `WTF/Config.wtf` afterwards has `realmlist` without a port, or the launcher has rewritten it
- Check: `savesctl.realm_restored`

#### Scenario: Control test actions are refused outside tests
- **GIVEN** a normal (non-test) `./play` or app launch
- **WHEN** `HS_GLUE_TEST` carries `ctl:` or `realm:` actions without the harness's test flag
- **THEN** the launch refuses, as other test switches already do, and the PLAYER account never sends a control token from a test script
- Check: `savesctl.test_actions_refused`
