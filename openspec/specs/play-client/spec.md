# play-client Specification

## Purpose
A revertable, patched copy of the client that the game actually runs, built from the pristine `client/` (ADR-0009).

## Requirements

### Requirement: Derived play client from a hash-checked queue
`harness/hs client-build` SHALL build `env/client-play/` from `client/` plus `patches/client/`, deterministically, without ever writing to `client/`.

#### Scenario: Build is deterministic and leaves the base untouched
- **GIVEN** the pristine `client/` matching `env/client-manifest.json`
- **WHEN** `hs client-build` runs twice
- **THEN** both runs produce identical output hashes recorded in the build stamp, the patched Wow.exe's SHA-256 equals patch 0001's recorded output hash, and `client/` is byte-identical to its manifest before and after
- Check: `client.play_build`

#### Scenario: Wrong base or double apply is refused
- **GIVEN** a base whose Wow.exe hash or original bytes differ from patch 0001's record (including an already-patched exe)
- **WHEN** the patch is applied
- **THEN** the build stops with an error naming the mismatch and writes nothing
- Check: `client.play_build`

#### Scenario: Revert gives back stock
- **GIVEN** a built play client
- **WHEN** it's rebuilt with patch 0001 removed from the queue
- **THEN** its Wow.exe is byte-identical to `client/Wow.exe`
- Check: `client.play_revert`

#### Scenario: Patch bytes are documented
- **GIVEN** patch 0001's record
- **WHEN** the harness reads it
- **THEN** it names the offset(s), original and new bytes, input/output SHA-256, and the ADR-0009 appendix cites the same values and the disassembly addresses
- Check: `client.patch0001_recorded`

### Requirement: Stock mode one setting away
With `client = stock`, `./play` and the .app SHALL launch the pristine `client/` exactly as before this change.

#### Scenario: Stock mode launches the untouched client
- **GIVEN** `client = stock` in a scratch settings file
- **WHEN** the play loop runs with the stand-in check client path replaced by the real Wow.exe launch smoke
- **THEN** the launched executable is `client/Wow.exe` (pristine hash), and with `client = patched` it's `env/client-play/Wow.exe` (patched hash)
- Check: `client.stock_mode`
