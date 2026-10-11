---
name: hs-reverse-engineering
description: Read-only reverse engineering of the 3.3.5a client with Ghidra via `harness/hs re`. Use to answer how the client behaves internally (file/signature checks, crash sites, what a patch would touch) without modifying it.
---

# Read-only client RE (ADR-0005)

**Rule:** analysis only. Every analysis runs on a copy in `env/re/bin`; `client/` is never written. Proposing any binary change needs an ADR and Zach's explicit yes (§2/§11). `re.ghidra_readonly` proves `client/` is unchanged.

## Setup
- `brew install openjdk@21` (keg-only), then `harness/hs ghidra-install` (pinned version, URL and SHA-256 in fork.lock; unpacks to `env/ghidra_<ver>_PUBLIC`).
- Ghidra rejects project paths with a `.`-prefixed folder (e.g. `.claude/worktrees/...`); set `HS_RE_DIR` to a plain path when working from a worktree.

## Commands
- `harness/hs re import [--analyze] [--name Wow.exe]`: import a copy into the shared project `env/re/projects`. `--analyze` runs full auto-analysis (slow, once), which decompile and xrefs need.
- `harness/hs re strings "<text>"`: ASCII search with addresses (works without analysis).
- `harness/hs re xrefs 0x<addr>` / `harness/hs re decompile 0x<addr>`: who references an address; decompile the containing function.
- Reports land in `env/re/out/<binary>.<mode>.txt`; the script is `harness/ghidra_scripts/HsReport.java` (read-only by design; opens the project `-readOnly` for queries).

## Workflow
1. Find anchor strings (error messages, file names) → `strings`.
2. `xrefs` on the string address → the referencing functions.
3. `decompile` those functions; follow callers with more xrefs.
4. Write findings with provenance: the binary's SHA-256 (first line of each report), the function addresses, and a behaviour description. Don't commit decompiled code; short excerpts in docs are fine (the repo is public: keep them short, attributed and analytical, ADR-0005).

## Known anchors (Wow.exe 3.3.5.12340)
- "GlueXML is modified or corrupt" @0x009f42c4, "GlueXML has corrupt signature" @0x009f42e4, "GlueXML missing signature" @0x009f4304, "GlueXML\GlueXML.toc" @0x009f432a. Patch `patches/client/0001-gluexml-signature.json` (ADR-0009) is built on these.
