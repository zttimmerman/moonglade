# ADR-0005: Ghidra read-only analysis of the client is allowed; patching stays gated

> Project renamed to Moonglade on 2026-10-05 (openspec/changes/rename-moonglade); this record keeps the name it was written under.

- Status: accepted (opened by Zach, 2026-10-02)
- Date: 2026-10-02

## Context
CLAUDE.md §2 originally said "No client binary patching in v1. Ghidra work is out of scope until a human explicitly opens it." Several open questions are about how the 3.3.5a client behaves internally, and those are best answered by reading its code rather than by trial and error:
- whether the client verifies its GlueXML/login-screen files (the login-screen spike, a fast follow after M1)
- crash diagnosis (e.g. the ILLEGAL_INSTRUCTION at a non-module EIP)
- what WoWSilicon's performance patches actually change

## Decision
- **Allowed:** read-only reverse engineering of the client with Ghidra: disassembly, decompilation, string/xref search, and headless scripts that only report. Ghidra is pinned in `fork.lock` (version, URL, SHA-256) and unpacked into `env/` (gitignored), with JDK 21 from Homebrew (`openjdk@21`, keg-only). The harness command is `hs re …`.
- **Read-only, by construction:** analysis runs on a copy of the binary under `env/re/`, never on `client/`. Ghidra projects and outputs live under `env/re/`. The check `re.ghidra_readonly` proves `client/` is unchanged after an analysis, and `client.inspect` keeps guarding it.
- **Still gated (§2/§11):** any change to a client binary, whether patching `Wow.exe`/DLLs, loader patches, or code injection. Proposing one needs an ADR and Zach's explicit yes. Analysis findings may *inform* such a proposal; they never apply it.
- **Provenance:** findings cite the analyzed file's SHA-256 and the function addresses, the same way data changes cite their sources (§7).

## Consequences
- About 1 GB of disk (Ghidra plus a JDK, plus projects).
- Faster, evidence-based answers for client questions.
- No redistribution: analysis artifacts stay local under `env/re/`, and nothing derived from the binary is committed except short findings in docs (addresses, behaviour descriptions).
