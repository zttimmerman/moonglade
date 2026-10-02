# Spec Delta: client-analysis

## Purpose
Answer questions about how the 3.3.5a client works by reading its code with Ghidra, headlessly and without ever modifying the client.

## ADDED Requirements

### Requirement: Pinned, verified analysis tooling
`harness/hs ghidra-install` SHALL install exactly the Ghidra build pinned in `fork.lock`, refusing on a SHA-256 mismatch, and SHALL report the JDK it will use.

#### Scenario: Install is pinned and idempotent
- **GIVEN** Ghidra is installed under `env/`
- **WHEN** `harness/hs ghidra-install` runs again
- **THEN** it exits 0 without downloading, reports the pinned version and a JDK 21+ path, and the installed tree carries a marker equal to the pinned SHA-256
- Check: `re.ghidra_installed`

#### Scenario: Mismatch and unverified trees are refused
- **GIVEN** a pin whose SHA-256 doesn't match the downloadable archive, or an existing Ghidra tree with no install marker
- **WHEN** the install runs (against scratch locations)
- **THEN** the mismatched download is refused with "SHA-256 mismatch" and nothing is unpacked; the unmarked tree is refused, and no marker is written for it
- Check: `re.ghidra_refuses_mismatch`

### Requirement: Read-only analysis
`harness/hs re` SHALL analyze only copies of client binaries under `env/re/`, and SHALL leave every file under `client/` byte-identical.

#### Scenario: Headless analysis leaves the client untouched
- **GIVEN** the installed client and its manifest
- **WHEN** the harness imports `Wow.exe` with `hs re import` and runs a headless report script (e.g. listing the PE entry point and searching strings for "GlueXML")
- **THEN** the report contains the entry point address and at least one string match with its address; the Ghidra project and copy are under `env/re/`, and the file Ghidra reports it opened is the copy under `env/re/`, never under `client/`; and every `.exe`/`.dll` under `client/` has the same SHA-256 and mtime as before. No file outside the game's own runtime-written paths (`WTF/`, `Logs/`, `Cache/`, `Errors/`, `Screenshots/`, `mtld3d_shaders.bin`) is added, removed, or changed
- Check: `re.ghidra_readonly`
