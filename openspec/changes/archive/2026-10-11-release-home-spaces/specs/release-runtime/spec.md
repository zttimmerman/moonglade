## ADDED Requirements

### Requirement: The servers' OpenSSL works without Homebrew
The release SHALL bundle OpenSSL's legacy provider (`server/lib/legacy.dylib`, from the openssl@3 the servers link) and start the servers with `OPENSSL_MODULES` pointing at it, so the world session cipher (RC4, AzerothCore `ARC4.cpp`) works on a Mac without Homebrew.

#### Scenario: Legacy provider and RC4 load with Homebrew hidden
- **GIVEN** the release stack started from the payload under the sandbox that hides `/opt/homebrew` and `/usr/local`
- **WHEN** the payload's `libcrypto.3.dylib` is probed under the same sandbox with the running worldserver's `OPENSSL_*` environment, and again without `OPENSSL_MODULES`
- **THEN** the first probe loads the legacy provider and fetches RC4, and the second loads neither (the counterexample proves the bundled module is the one used)
- Check: `release.stack_from_payload`
