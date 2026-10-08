# Moonglade — standing rules

Offline single-player WoW (AzerothCore 3.3.5a) on macOS ARM64. These rules apply to every session. On resume, read `STATUS.md` and `openspec/` first. Remaining kickoff material (Gate 1, milestones, first-session plan) lives in `KICKOFF.md`; the M0 harness requirements (original section 5) live in `docs/harness-requirements.md`.

## 1. Goal

A single-player, fully offline World of Warcraft experience on macOS (Apple Silicon). One command boots a local server and launches the client. The world feels populated by AI players. Dungeons are completable solo or with bot companions. An optional "era profile" restores pre-2.3 (vanilla-style) leveling pace and difficulty.

**Target:** the target game is WotLK 3.3.5a (build 12340): all of its content and systems through level 80, including Outland, Northrend, and Death Knights. This is not a vanilla project. The era profile only adjusts 1–60 pacing and difficulty; it never removes or replaces WotLK content or systems.

This is a **personal project**. Its source is public under GPL-2.0-or-later; no client or game files (client binaries, patched files, repacks) are distributed (ADR-0012).

## 2. Fixed decisions (do not relitigate without a human gate)

- **Game version:** WotLK 3.3.5a, build 12340.
- **Server core:** AzerothCore (WotLK 3.3.5a), forked. Changes go in modules and SQL where possible, core patches only when unavoidable, and each core patch gets an ADR.
- **Bots:** mod-playerbots. Scaling: a SoloCraft-style module (evaluate the existing one before writing new).
- **Client:** the Windows 3.3.5a (build 12340) client, run under CrossOver/Wine. The native Mac 3.3.5a client is 32-bit and unusable on modern macOS.
- **Host:** macOS ARM64. Docker is acceptable for MySQL and/or the server if native builds fight you; record the choice in an ADR.
- **No client binary patching in v1.** Use config (`Config.wtf`, `realmlist.wtf`) and a launcher script instead. **Read-only** Ghidra analysis of the client is open (Zach, 2026-10-02, ADR-0005): analyze copies under `env/re/`, never modify `client/`. Any binary change still needs an ADR and Zach's explicit yes. **Revertable client patching is open via ADR-0009** (Zach, 2026-10-04): `client/` stays pristine; patches apply only to the derived play client `env/client-play/` from the hash-checked queue `patches/client/`. ADR-0009 approves the mechanism and the first patch (GlueXML signature check) only; every further binary patch needs its own ADR and Zach's yes.

## 4. Methodology: OpenSpec (brownfield)

This is delta work on an existing codebase, so use OpenSpec:

- Every unit of work is a change in `openspec/changes/<id>/` with a proposal, spec deltas, and tasks.
- Every requirement has at least one **scenario** (GIVEN/WHEN/THEN).
- **Every scenario maps to an automated check in the harness** (section 5), or is explicitly tagged `[human-verify]` with a reason. A scenario with neither is incomplete, and the change cannot be approved.
- Archive a change only after its verifier pass (section 6).

## 6. Roles and the verifier rule

- **You (orchestrator):** specs, task breakdown, delegation, integration, STATUS.md, ADRs.
- **Builder subagents:** implement one task against one spec, with a clean context per task.
- **Verifier subagent:** a fresh context that did not build the change. It receives only the spec deltas, the diff, and harness access. It returns PASS or FAIL per scenario with evidence.

**Builders never grade their own work. You never accept a builder's claim of completion without a verifier PASS.**

## 7. Data provenance (critical)

Never write game data values (XP tables, creature ranks, quest rewards, spell values, mount levels) from your own recall. Your memory of WoW data is unreliable. Every data change must cite a source:

- For pre-2.3 values, the primary source is the **VMaNGOS world database** (1.12). Write a diff tool that compares VMaNGOS tables against AzerothCore's for the same entities (by entry ID) and generates the change set from the diff.
- Secondary sources: AzerothCore's own SQL history and well-known wiki patch notes, cited in the change proposal.
- If you cannot source a value, flag it in the proposal instead of guessing.
- VMaNGOS is a read-only data reference for the era profile, never a codebase to adopt or a version to target.

## 9. The work loop

Repeat until the current milestone's exit criteria are met:

1. Read STATUS.md and the open changes.
2. Pick the highest-priority unblocked task.
3. Delegate to a builder with the spec deltas and task.
4. Send the result to the verifier.
5. FAIL: return to the builder with the verifier's evidence. After 3 failed cycles on one task, mark it blocked with a written diagnosis and move to another task. Do not stop the session.
6. PASS: integrate, run the **full** harness to catch regressions, update STATUS.md, archive the change if all its tasks are done.
7. When the milestone's exit criteria are all green, stop at its human gate if it has one; otherwise plan the next milestone's changes and continue.

## 10. What does NOT count as done

None of these is evidence of completion:

- "It compiles," "it launches," or "it seems to work."
- A builder's summary of its own work.
- Passing a subset of the harness.
- A scenario you rewrote or loosened to make it pass. Changing a spec requires a recorded reason in the change proposal and is reviewed at the next human gate.

If you notice yourself concluding the project is "good enough," check section 8. If an exit criterion is not green, it is not done.

## 11. When to stop and wait for the human

Stop only for: a human gate in section 8, Gate 1, any action involving client binary modification, anything that would delete or overwrite user data outside the repo, or a fixed decision (section 2) that you believe is wrong. In that last case, write an ADR arguing the change and wait.

Everything else, including blocked tasks, ambiguous specs, and failing tests, you handle by recording it in STATUS.md and continuing with other work.

## 12. State files

- `STATUS.md`: current milestone, task board (todo / in progress / blocked / done), last full harness result, open questions for Zach. Keep it short and current. It is how the next session resumes.
- `docs/adr/`: one file per significant decision.
- `openspec/`: specs and changes as above.
