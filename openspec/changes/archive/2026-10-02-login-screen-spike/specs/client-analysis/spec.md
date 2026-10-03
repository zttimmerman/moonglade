# Spec Delta: client-analysis

## ADDED Requirements

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
