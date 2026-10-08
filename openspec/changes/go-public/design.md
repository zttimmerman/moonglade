## Decisions
- Option B from the audit (rewrite this repo), per Zach's 2026-10-06 decision, not the audit's option C.
- The denylist lives in `env/public-denylist.txt` (untracked, 0600), so the personal strings themselves are never committed. The check fails closed without it. A friend's clone has no denylist, so the check SKIPs there with a reason; it runs on Zach's Mac and in verification.
- `git filter-repo` is needed for the rewrite. Install it into a repo-local venv (`env/tools/venv`, pip) or vendor the single script under `tools/go-public/`. Never Homebrew.
- No SPDX headers per file in this change (they would conflict with in-flight harness work). LICENSE + NOTICE cover the repo; headers can follow later.
- STATUS.md, KICKOFF.md, CLAUDE.md, .claude/ and the openspec archive are published after the scrub. Decision records keep "Zach" as attribution (normal, his call: kept).
- Builder works in a git worktree (no runtime folders). Harness checks run later in the main checkout.
