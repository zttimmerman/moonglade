---
name: hs-change-loop
description: Hearthstone's unit-of-work loop. OpenSpec change → branch → builder → verifier → full run → PR/merge → archive. Use when starting, delegating, verifying, or closing any change in this repo.
---

# Hearthstone change loop

CLAUDE.md §4/§6/§9 are the rules; this is the practiced procedure.

## Start a change
1. `export PATH=~/.nvm/versions/node/v20.20.1/bin:$PATH` (openspec needs Node 20), then `openspec new change <id>`.
2. Write proposal.md, `specs/<capability>/spec.md`, design.md and tasks.md. Every scenario needs a `Check: \`<id>\`` line or `[human-verify]` plus a reason. Data values cite sources (§7), never memory. Explain user-facing choices in plain gameplay language.
3. `openspec validate <id>`.
4. `git checkout -b change/<id>` from `main`; add `<id>` to `harness/active-changes`; commit; `git push -u origin change/<id>`.

## Delegate
- One builder per task group, with clean context. The brief names the spec files, the files it may edit, and the stack rules: who owns the stack, never run `hs down/build` unless it owns it, use `--leave-up` when sharing. It must not edit specs, must commit on the branch, and must not merge or push.
- Builders that need runtime (server, client, Wine) work in the main checkout; only one agent owns the stack at a time. Research or docs builders can use `isolation: "worktree"`.
- A builder's report is not evidence (§10).

## Verify
- Use the `verifier` agent type and give it only: spec deltas, `git diff <last-verified>..HEAD -- harness/ …`, and the harness command.
- **Run budget (agreed with Zach, 2026-10-02): 2 full runs per change.** My pre-check is 1 full run (the quick tier once tiers land); the verifier does **1 full run** plus **≥5 repeats of only the checks the change adds or touches** (flakiness where it changed), plus counterexamples showing each new check can fail, plus independent checks outside the harness.
- **Harness-wide flakiness:** a separate "full ×3" before each milestone gate or about weekly, not per change. A failure in any run is P0.
- On FAIL: fix, run the full harness, then a **fresh** verifier (new context). After 3 failed cycles on one task, mark it blocked in STATUS.md and move on.
- Weakened checks count as FAIL. Spec changes need a dated entry in the proposal's "Spec-change log" with the reason; tightenings and clarifications are fine, loosenings get reviewed at the next human gate.

## Close
1. Tick tasks, then `openspec archive <id> --yes` (merges deltas into `openspec/specs/`). Confirm `Check:` lines survived (`grep -c "Check: \`" openspec/specs/*/spec.md`).
2. Remove `<id>` from `harness/active-changes`; update STATUS.md (board, last full run, follow-ups).
3. `gh pr create` with the verifier verdict in the body (end with the Claude Code attribution line), then `gh pr merge <n> --merge --delete-branch`, then `git checkout main && git pull`.

## Gotchas
- `report.coverage` fails if a change has ticked tasks but isn't in `harness/active-changes`.
- macOS `sed` lacks GNU `0,/re/`; use Python for scripted edits.
- `git stash` is shared across worktrees; prefer WIP commits.
- With several worktrees open, never rely on the shell's cwd for git writes: use `git -C <path>` (the cwd can reset to the main checkout between commands). A commit once landed on a branch mid-verification this way.
