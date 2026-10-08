## Why
Zach wants to share the repo with friends (2026-10-08: "I want to share the repo"). The public-readiness audit (B28, `research/public-readiness` 9064140, `docs/research/public-readiness-audit.md`) found no secrets but personal data, a missing licence, stale "private" wording, two Blizzard screenshots and a self-hosted-runner plan that is unsafe in public. Zach's decisions (2026-10-06):
- keep this repo and rewrite its history (noreply email, strip Claude session links, Hearthstone → Moonglade in messages, drop .pyc, cut personal lines from old STATUS versions);
- force-push, edit PR bodies, ask GitHub Support to purge the `refs/pull/*` refs, flip public only after verifying they're gone;
- publish everything after a scrub, including client patch 0001;
- keep the gold-ring icon;
- licence GPL-2.0-or-later;
- drop the two game screenshots.

## What Changes
- LICENSE (GPL-2.0-or-later), NOTICE (OpenSpec MIT files, VMaNGOS data attribution, "not affiliated with Blizzard" disclaimer, branding terms), README licence/disclaimer section.
- Tree scrub: personal data, home paths, [redacted]/runner plans, "private repo"/"never redistributed" wording (CLAUDE.md §1, README, docs/setup.md, ADR-0009 §6, ADR-0010), stale client docs, client-download pointers, F29 leftovers, `tools/rename-folder.sh`, `docs/research/img/*`. Hooks and skills made portable.
- A guard check that keeps the tree clean from now on, driven by a denylist kept outside git.
- History-rewrite tooling that runs only on a scratch mirror clone, plus a dry run proving the rewritten history is clean. The real rewrite, force-push, PR-body edits, Support request and visibility flip are **not** part of this change. Each is Zach's go, following `docs/going-public.md`.

Branding (`branding/`) is excluded from the code licence ("all rights reserved") so Zach keeps control of the logo. That's reversible later.

## Capabilities
### New Capabilities
- `public-repo`: licence, clean tree, clean-history tooling, go-public runbook.

## Impact
Docs, CLAUDE.md wording (§1 only, as the audit says), .claude hooks/agents wording, a few harness comments, new tools/go-public/, new checks. No game data, no client changes.

## Spec-change log
