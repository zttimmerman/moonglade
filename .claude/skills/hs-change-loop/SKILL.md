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

## Before any core or module patch (Zach, 2026-10-03)
Patches cost hours, so check upstream first:
1. Search issues **and PRs** in upstream `azerothcore/azerothcore-wotlk`, the playerbots core fork `mod-playerbots/azerothcore-wotlk` (branch `Playerbot`; our `core_repo`), and `mod-playerbots/mod-playerbots`, plus the relevant module repo for other modules. Use `gh search issues|prs "<terms>" --repo <owner/repo>`.
2. Check whether a newer version fixes it: `gh api repos/mod-playerbots/azerothcore-wotlk/compare/<fix-sha>...Playerbot` (behind_by > 0 means not synced yet), and compare our `fork.lock` pins with the latest heads.
3. Prefer, in order: (a) bump the pin to a version containing the fix; (b) cherry-pick the upstream commit into `patches/…`, citing it; (c) write our own patch. Record which, and the search results, in the ADR (e.g. ADR-0006's "Upstream status" section).
Lesson: the server-mail crash we spent ~1 h patching was already fixed upstream (AzerothCore #27827, merged 3 days after our pin).

## Delegate
- One builder per task group, with clean context. The brief names the spec files, the files it may edit, and the stack rules: who owns the stack, never run `hs down/build` unless it owns it, use `--leave-up` when sharing. It must not edit specs, must commit on the branch, and must not merge or push.
- Builders that need runtime (server, client, Wine) work in the main checkout; only one agent owns the stack at a time. Research or docs builders can use `isolation: "worktree"`.
- A builder's report is not evidence (§10).

## Builder pre-verification checklist (shift-left; Zach, 2026-10-04)
Builders run this before handing over and put a short pass/fail line per item in their report. It doesn't replace the verifier; it removes the predictable misses that cost whole verify rounds. Each item comes from a real verifier catch on this project.
1. **Test switches can't leak.** Every env var or flag a check uses is refused by normal entry points (`./play`, the app, `hs`) unless an explicit test flag is passed, and never applies to the PLAYER account. Prove it with a refusal run. (in-client-login: `HS_GLUE_TEST`.)
2. **No secrets in output.** Grep everything the change produces (`env/logs/*`, `results.json`, `summary.md`, `app.log`, check evidence, git history) for the PLAYER password (read it from `env/player.json` without printing it) and the test password. Credential files are 0600, never inside `client/`, removed on exit, and any stale copy is removed at start.
3. **Kill-safe.** SIGKILL the new flow mid-run: nothing sensitive or stale is left that the next run doesn't heal, and the next run works.
4. **Checks can fail, on this run's evidence.** Every new or changed check has at least one counterexample run that makes it FAIL. Evidence must come from this run (timestamps or pids newer than the run's start), never from stale or historical artifacts. (M2: the coalesced crash report.)
5. **Existing checks not loosened.** Any edit to an existing check is listed with why it isn't a loosening.
6. **GIVEN really holds.** Preconditions are established before the WHEN (M2: comp.assists summon order); "running server" scenarios observe the server, not just the DB.
7. **Fail-closed.** Self-verification code treats query or IO errors as failure, never as "0 differences" (M3: `Count()`).
8. **Data provenance (§7).** No hand-typed game values: names, classes, numbers come from VMaNGOS, AC base SQL or DBCs, with citations.
9. **Scope guards.** `client.inspect` passes; `env/player.json`, `env/settings.json` and PLAYER (last_login) are untouched; nothing is written outside the repo; TCC, bundle id and designated requirement are untouched; build inputs have changed only as intended (`--force-clean` if so).
10. **Repeat, and report oddities.** Each new or changed check passes ≥3 times. Any unexplained failure is reported as such, never dismissed as "probably a human" without confirming.
11. **Clean up.** Scratch dirs and test accounts removed, stack down, no stray Wine/Wow/server processes, disk checked.
12. **Docs match code.** design.md describes what was built (M-launcher: osascript vs AppKit drift).

## Verify
- Use the `verifier` agent type and give it only: spec deltas, `git diff <last-verified>..HEAD -- harness/ …`, and the harness command.
- **Run budget (Zach, 2026-10-05; replaces the 2026-10-02 two-full-runs rule): ONE full run per change, done by the verifier.** Builders run the **quick tier** plus **the checks the change adds or touches (≥3 repeats)** and their counterexamples, plus the pre-verification checklist. **No builder full run.** The verifier does the **single unfiltered full run** (`--force-clean` when build inputs changed), **≥5 repeats of the added/touched checks**, its own counterexamples, and independent checks outside the harness. If a verifier FAIL leads to a fix round, the builder again runs quick + affected only; the next verifier does the full run. Why: the builder's full run duplicated the verifier's (~25 min per change, ~50 with a fix round); the shift-left checklist covers what the builder run used to catch.
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
