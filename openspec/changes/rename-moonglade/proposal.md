# Proposal: Rename the project to Moonglade

## Why
"Hearthstone" is also Blizzard's card game. Zach (2026-10-05): WoW names are fine, but not another game's title. The new name is **Moonglade** (the druid sanctuary in WoW), with the repo and folder named `project-moonglade`.

## What Changes
1. **The app** becomes `Moonglade.app` (`dist/Moonglade.app`): bundle name and display name "Moonglade", bundle id `local.moonglade.app`, designated requirement `identifier "local.moonglade.app"`.
   - The Dock shim's display name becomes "Moonglade".
   - Notification names `local.hearthstone.*` become `local.moonglade.*`.
   - The new bundle id means macOS asks Zach for Documents access once more (one Allow click).
2. **User-facing and current text** says Moonglade:
   - launcher windows and dialogs;
   - `./play` / `[play]` / `[stage]` / `[shutdown]` messages;
   - the login screen's About page;
   - README, docs/setup.md, client-setup, CLAUDE.md, STATUS.md, skills, the verifier agent, and the current specs in `openspec/specs/`.
3. **History is left as written:** `openspec/changes/archive/`, `docs/research/`, ADR bodies (ADR titles stay; a one-line note in each that the project was renamed), and git history.
4. **Internal identifiers that aren't user-facing may stay:** `hs` CLI, `HS_*` env vars, `hslib`, `hs-` file prefixes. That keeps the change mechanical and low-risk.
5. **After merge, outside the harness:**
   - the GitHub repo is renamed to `project-moonglade`;
   - the local folder is renamed to `~/Documents/repos/project-moonglade` by Zach, with Claude Code closed, using a script that also moves Claude Code's project memory and settings to the new path.

## Capabilities
### Modified Capabilities
- `mac-app` (name and bundle id), `harness-reporting` (a naming guard).

## Spec-change log
- 2026-10-05: the `repo.naming` allowlist also covers three exact lines in build/patch inputs (comment headers of `modules/mod-era-profile/conf/mod_era_profile.conf.dist` and `src/EraProfile.cpp`, and the `"title"` of `patches/client/0002-glue-front-menu.json`). Renaming them would change build inputs (full rebuild) and a patch record for comment-only text no player sees. Exact-line allowlisting, not a path exemption. Follow-up: rename them in the next change that touches those inputs anyway.
