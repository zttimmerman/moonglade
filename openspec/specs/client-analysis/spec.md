# client-analysis Specification

## Purpose
Answer questions about how the 3.3.5a client works by reading its code with Ghidra, headlessly and without ever modifying the client.

## Requirements

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

### Requirement: Read-only MPQ access
`harness/hs mpq list <archive>` SHALL list the files inside a client MPQ, and `hs mpq extract <archive> <path> <dest>` SHALL extract one file to a destination outside `client/`. Neither SHALL modify anything under `client/`.

#### Scenario: List and extract without touching the client
- **GIVEN** the installed client
- **WHEN** the harness lists `Data/common.MPQ` and extracts one listed file into `env/re/`
- **THEN** the listing has more than 1000 entries, the extracted file exists with nonzero size, and every file under `client/` outside the game-written paths is unchanged (same snapshot rule as `re.ghidra_readonly`)
- Check: `re.mpq_readonly`

### Requirement: MPQ writing never targets the client
`harness/hs mpq write` SHALL write archives that the read-only reader reads back identically, and SHALL refuse any destination under the client directory.

#### Scenario: Round trip, and refusal tested without touching the real client
- **GIVEN** a scratch directory standing in for the client, plus a scratch output directory
- **WHEN** the harness writes an archive with a few files (stored and zlib) to the output directory, reads it back, and then attempts a write into the stand-in client directory
- **THEN** every file reads back byte-identical, the write into the stand-in client is refused with nothing created, and the real `client/` is never a write target at any point in the check (its snapshot outside game-written paths is unchanged, including when the refusal guard is sabotaged in a counterexample)
- Check: `re.mpq_write_roundtrip`

### Requirement: Spike findings for the front menu
The spike SHALL answer Q1 and Q2 with evidence: Ghidra addresses and behaviour for the GlueXML verification; MPQ listings and paths for the backdrops; and the result of a controlled launch test, with screenshots and log excerpts. It SHALL end with a recommendation and any decisions Zach must make.

#### Scenario: Zach reviews the spike report
- **GIVEN** `docs/research/login-screen-spike.md`
- **WHEN** Zach reads it
- **THEN** they can decide whether the front menu proceeds as a data-only change, needs an executable change (with its risks), or is descoped; and whether the background picker is feasible
- [human-verify] The deliverable is a decision for Zach; its quality is judged by them.
