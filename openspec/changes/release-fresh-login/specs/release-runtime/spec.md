## ADDED Requirements

### Requirement: A fresh Mac can log in to the release world
A client SHALL be able to log in to the world served from the release payload on a Mac without Homebrew whose Moonglade folder path contains a space.

#### Scenario: A fresh account reaches the character list
- **GIVEN** the release stack started from the payload with a scratch HOME whose path contains a space, the servers running under the sandbox that hides `/opt/homebrew` and `/usr/local`, and a throwaway account
- **WHEN** the harness's protocol client logs in: auth (SRP6), the realm list, the world's AUTH_SESSION, and CMSG_CHAR_ENUM
- **THEN** every step succeeds and SMSG_CHAR_ENUM is received, the account is removed afterwards, and with the servers started without `OPENSSL_MODULES` (one-off counterexample) the check fails at the world step
- Check: `release.stack_from_payload`
