# Proposal: Login-screen spike (feasibility for the front menu)

## Why
Zach wants the 3.3.5a login screen to become the game's front menu: profiles/saves/options, auto-login per profile, the original animations kept, and optionally a WotLK/TBC/Vanilla background picker (see STATUS backlog, "Front menu"). Before building it we need evidence for two questions:
- **Q1:** can the client load a custom GlueXML (login-screen UI) from a patch MPQ with an **unmodified** `Wow.exe`? Read-only Ghidra already found "GlueXML is modified or corrupt", "GlueXML has corrupt signature" and "GlueXML missing signature" in `Wow.exe` (ADR-0005), so the client verifies something.
- **Q2:** do the 3.3.5a MPQs still contain the TBC and Vanilla login backdrops (models plus music) for a background picker?

## What Changes
- A read-only MPQ reading tool for the harness (`hs mpq list|extract`), installed pinned or implemented from a documented source. It never writes into `client/`.
- Ghidra analysis of the GlueXML verification path (`hs re`), with addresses and behaviour.
- A controlled launch test for Q1, using a throwaway patch MPQ that is **only added to a scratch copy of `client/`'s Data folder**, or removed again with `client.inspect` proving restoration. The patch MPQ is a data layer, not a binary change (ADR-0005/§2); no `Wow.exe` change is attempted.
- A spike report, `docs/research/login-screen-spike.md`, with evidence and a recommendation, for Zach.

## Capabilities
### New Capabilities
(none; spike)

### Modified Capabilities
- `client-analysis`: adds read-only MPQ listing/extraction.

## Impact
Research only. If Q1's answer is "an exe change is needed", the report says so and stops: any binary change needs an ADR and Zach's yes.

## Spec-change log
(none yet)
