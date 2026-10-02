# Tasks

## 1. Tooling (orchestrator)
- [x] 1.1 ADR-0005, CLAUDE.md §2 update, fork.lock pins — verify: files present
- [x] 1.2 `hs ghidra-install`, `hs re …` (copy-based, headless) + checks — verify: `re.ghidra_installed`, `re.ghidra_readonly`
## 2. Skills (orchestrator)
- [x] 2.1 `.claude/skills/hs-change-loop`, `hs-harness-checks`, `hs-client-debugging`, `hs-reverse-engineering` — verify: each has frontmatter name/description, and they're listed by Claude Code
## 3. Verify
- [x] 3.1 Verifier pass + full harness (cycle 1 PASS with a strong concern: self-written install marker → fixed e6cb591; cycle 2 PASS, 1 full + 5x re.*, 2026-10-02)
