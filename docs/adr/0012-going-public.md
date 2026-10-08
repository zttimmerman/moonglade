# ADR-0012: Publish the Moonglade source

- Status: **accepted** (Zach, 2026-10-06 decisions after the public-readiness audit; 2026-10-08: "I want to share the repo").
- Source: `docs/research/public-readiness-audit.md` (B28, branch `research/public-readiness` 9064140), change `go-public`.

## Context
CLAUDE.md §1 said Moonglade is a personal project whose output nobody else receives, and the repo was private. Zach wants friends to use it with their own 3.3.5a client.

## Decision
- The **source** is published under GPL-2.0-or-later (matches AzerothCore/mod-playerbots/VMaNGOS terms; "or later" keeps the AGPL-3.0 combined server binary legal). Branding stays all rights reserved.
- **No client or game files are ever distributed** ("bring your own client" is absolute; no links to client downloads). Client patches stay byte recipes keyed by the stock hash; patch 0001 is published.
- CLAUDE.md §1 is reworded to say exactly that. §2 fixed decisions are unchanged.
- This repo is kept and its history rewritten (noreply email, no session links, no .pyc/screenshots, personal lines scrubbed). GitHub Support purges `refs/pull/*`. The visibility flip comes only after that's verified. Each irreversible step needs Zach's explicit go at the time (`docs/going-public.md`).
- Self-hosted runners are never attached to the public repo.

## Consequences
- `public.*` checks keep the tree clean (denylist kept outside git).
- Future prebuilt releases carry GPL source obligations (tagged source alongside).
