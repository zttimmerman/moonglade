## Why
v0.1.0 (a HOME path with a space, rejected by the servers' config parser) and v0.1.1 (the world server's RC4 needed OpenSSL's legacy provider, missing without Homebrew) both reached a user. Each release check covered one piece, but none did what a fresh Mac does first: log in to the world with the servers running under a spaced HOME and with Homebrew hidden.

## What changes
- `release.stack_from_payload`: after the stack is up from the payload (spaced scratch HOME, servers under the no-Homebrew sandbox), a throwaway account (SOAP `account create`) logs in with the harness's protocol probe through auth, the realm list, the world's AUTH_SESSION (the server's RC4 starts there) and the character list, and is deleted afterwards. The sandbox proof now also shows `/opt/homebrew` unreadable.
- `stack.spawn_env`: a release test switch `drop_openssl_modules` (honoured only with the test flag inside a live harness run) for a one-off counterexample: `HS_TEST_STACK_NO_OPENSSL_MODULES=1 harness/hs run --only release.stack_from_payload` starts the servers without `OPENSSL_MODULES`, and the check fails at the world step.

## Impact
- `harness/hslib/checks/release_runtime_checks.py`, `harness/hslib/stack.py`, `harness/hslib/firstlaunch.py` (docstring), a unit test.

## Spec-change log
- 2026-10-11: added "A fresh Mac can log in to the release world" (a tightening).
