# ADR-0001: Core base is the mod-playerbots AzerothCore fork, kept as pinned SHA + patch queue

> Project renamed to Moonglade on 2026-10-05 (openspec/changes/rename-moonglade); this record keeps the name it was written under.

- Status: accepted
- Date: 2026-10-01

## Context
Fixed decision §2: AzerothCore (WotLK 3.3.5a), forked, with mod-playerbots. The mod-playerbots README states that all installations require `mod-playerbots/azerothcore-wotlk` (branch `Playerbot`); upstream `azerothcore/azerothcore-wotlk` will not work (checked 2026-10-01, README on `master`).

## Decision
- Core base = `mod-playerbots/azerothcore-wotlk@Playerbot`, pinned by SHA in `fork.lock`.
- "Our fork" lives in this repo, not on GitHub: pinned SHA + `patches/core/*.patch` (each with its own ADR) + our modules under `modules/`. `harness/hs build` checks out the pinned SHA into `vendor/` (gitignored), applies patches, links modules.
- mod-playerbots pinned by SHA the same way.

## Alternatives
- A GitHub fork under Zach's account: it's outward-facing and can't be reviewed in one diff. Can be added later without changing this layout.
- Git submodule: no better than a lockfile, and patches still need a home.

## Consequences
Upgrading = bump SHAs, re-apply patches, run full harness. A patch that fails to apply fails the build loudly.
