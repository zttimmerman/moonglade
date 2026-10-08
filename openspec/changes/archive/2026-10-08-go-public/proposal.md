## Why
Zach wants to share the repo with friends (2026-10-08: "I want to share the repo"). The public-readiness audit (B28, `research/public-readiness` 9064140, `docs/research/public-readiness-audit.md`) found no secrets but personal data, a missing licence, stale "private" wording, two Blizzard screenshots and a self-hosted-runner plan that is unsafe in public. Zach's decisions (2026-10-06):
- keep this repo and rewrite its history (noreply email, strip Claude session links, old project name → Moonglade in messages, drop .pyc, cut personal lines from old STATUS versions);
- force-push, edit PR bodies, ask GitHub Support to purge the `refs/pull/*` refs, flip public only after verifying they're gone;
- publish everything after a scrub, including client patch 0001;
- keep the gold-ring icon;
- licence GPL-2.0-or-later;
- drop the two game screenshots.

## What Changes
- LICENSE (GPL-2.0-or-later), NOTICE (OpenSpec MIT files, VMaNGOS data attribution, "not affiliated with Blizzard" disclaimer, branding terms), README licence/disclaimer section.
- Tree scrub: personal data, home paths, self-hosted infrastructure/runner plans, "private repo"/"never redistributed" wording (CLAUDE.md §1, README, docs/setup.md, ADR-0009 §6, ADR-0010), stale client docs, client-download pointers, F29 leftovers, `tools/rename-folder.sh`, `docs/research/img/*`. Hooks and skills made portable.
- A guard check that keeps the tree clean from now on, driven by a denylist kept outside git.
- History-rewrite tooling that runs only on a scratch mirror clone, plus a dry run proving the rewritten history is clean. The real rewrite, force-push, PR-body edits, Support request and visibility flip are **not** part of this change. Each is Zach's go, following `docs/going-public.md`.

Branding (`branding/`) is excluded from the code licence ("all rights reserved") so Zach keeps control of the logo. That's reversible later.

## Capabilities
### New Capabilities
- `public-repo`: licence, clean tree, clean-history tooling, go-public runbook.

## Impact
Docs, CLAUDE.md wording (§1 only, as the audit says), .claude hooks/agents wording, a few harness comments, new tools/go-public/, new checks. No game data, no client changes.

## Spec-change log
- 2026-10-08 (orchestrator rule, builder edit) · public-repo "Tracked tree is free of personal data": the denylist is required (missing or empty → error, fail-closed) only when the untracked marker `env/public-denylist.required` exists; otherwise the denylist part is skipped, the built-in patterns still run, and the evidence says so. Reason: friends' clones have no denylist and must not error; the maintainer's machine has the marker, so it is still fail-closed there. Requirement text and the "A planted hit fails" scenario updated to match.
- 2026-10-08 (builder) · **Removed** harness-reporting "Folder rename is safe" (`repo.rename_script`) from `openspec/specs/harness-reporting/spec.md`, together with the check and its timing-baseline entry. Reason: this change deletes `tools/rename-folder.sh` (audit §7: a one-off with the dev Mac's paths; the folder rename is done), and a scenario can't map to a check for a deleted script. No remaining scenario is loosened. The naming guard's self-test now plants its line-allowlist case in the harness-reporting spec instead of the deleted script; `repo.portable` lost two allowlist entries (stricter).
- 2026-10-08 (builder) · public-repo wording only: the session-link pattern is named "Claude Code session URLs" and the old project name "the naming guard's `OLD_NAME`", so this change's own files pass `public.tree_scrub` and `repo.naming` without new allowlist entries. Meaning unchanged.
- 2026-10-08 (builder) · Clarification: the history scan uses the personal patterns and the denylist; the wording patterns ("private repo", "never redistributed") are tree-only, because old file versions may say what was true then and the rewrite doesn't edit old wording.
- 2026-10-08 (verifier cycle 1 FAIL): merging main brought in second-stack's two-lane timing baseline join, which still listed the retired `repo.rename_script`. Removed it from both baseline sections so `report.timing` doesn't expect a check that no longer exists. Clarification: `public.history_dryrun` compares the rewritten tree of the branch under test with its HEAD, and reports `main`'s trees. After the merge, that branch is `main`, so this is equivalent to the spec's "`main`'s tree hash".
