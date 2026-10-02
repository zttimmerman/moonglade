# Spec Delta: server-build

## ADDED Requirements

### Requirement: Login/logout race doesn't crash the world server
The built worldserver SHALL survive a character logging out before its login-time server-mail lookups complete.

#### Scenario: Immediate logout after login, repeated
- **GIVEN** a running server built with the core patch queue, and the test character with instant logout
- **WHEN** the probe logs the character in and requests logout immediately (no settle wait), 20 times in a row
- **THEN** worldserver is still running with the same pid, SOAP answers, no new crash report for worldserver exists, and every logout completed
- Check: `core.login_logout_race`

#### Scenario: The race reproduces without the patch
- **GIVEN** a scratch build of the pinned core **without** `0001-server-mail-guard-async-player.patch` (built in a separate build dir; the real install is untouched)
- **WHEN** the same immediate login/logout loop runs against it
- **THEN** that worldserver crashes within the 20 attempts (proving the check exercises the real defect), and the evidence includes the crash report's top frames naming `ServerMailMgr::SendServerMail` or the `server_mail.cpp` callback
- Check: `core.login_logout_race_baseline`

#### Scenario: Patch is applied by the pinned build
- **WHEN** `harness/hs build` syncs the vendor tree
- **THEN** `0001-server-mail-guard-async-player.patch` applies cleanly to `core_sha`, the patched source contains the GUID guard, and the build-input hash changes when the patch file changes
- Check: `core.patch_applied`
