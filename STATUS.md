# STATUS

_Last updated: 2026-10-02 (session 2)_

## Current milestone: M0 Foundation
Change: `openspec/changes/m0-foundation-harness/` (proposal, 5 spec deltas, design, tasks).

## Task board
| Task | State | Notes |
|---|---|---|
| M0 tasks 1.1–8.1 | **done, verifier PASS** | cycle 1 FAIL (`build.playerbots_active` couldn't fail) → fixed `fbad133` → cycle 2 PASS |
| 10.1/10.2 client install + Wine launch (B5) | done, awaiting verifier | zip repaired by Zach; installed (manifest written, Wow.exe 3.3.5.12340). `client.inspect` + `client.launch_smoke` PASS (smoke now also requires an on-screen ≥640×480 window). Screenshot confirmed the WotLK intro cinematic renders under Whisky's Wine |
| 9.2 M0 human gate | **PASSED 2026-10-01** | Zach: logged in under Whisky's Wine, walked around Goldshire, "Game works". Fixes on the way: Warden disabled (ILLEGAL_INSTRUCTION crash at login); EULA must be scrolled to enable Accept |
| F3 red/blue smears in world | **fixed, pending verifier** | Root cause: a Wine 7.7 wined3d bug (clip-plane GLSL emits undeclared `ps_link`). Default layer is now WoWSilicon's Wine runtime r17 + mtld3d (D3D9→Metal), with no client patching (ADR-0004 rev). Zach confirmed in-game: no smears, faster |
| F4 `hs wine-install` | done | adopt/idempotent paths tested; download-from-scratch path not exercised (64 MB) |
| F5 mtld3d shader cache written into `client/`; slight colour banding (HDR on) | todo (low) | |
| F1 pin enforcement vs gitignored content | done | rogue module dir + ignored files now removed; sabotage via empty commit (check 326 s → 11 s) |
| Gate 1 | **SIGNED 2026-10-01** | vanilla XP curve, vanilla quest XP, elites + HP, vanilla mounts; vanilla mode ON by default and easily toggleable; descoped items backlogged |
| F2 `report.full_run` proves files written only externally | todo (low) | verifier concern 2 |
| M1 `./play` | spec written (`openspec/changes/m1-offline-play-loop`) | next after M0 archives |
| M2 living world | spec written (`openspec/changes/m2-living-world`) | population, companions, perf budget |
| M3 vanilla mode | spec written (`openspec/changes/m3-vanilla-mode`) | module + side tables per signed Gate 1 |
| F6 setup reproducibility | todo | Brewfile, `hs bootstrap`, `docs/setup.md`. Private GitHub remote: done 2026-10-02 |

## Last full harness result
2026-10-01, HEAD `fbad133`, Release build: **25 pass / 0 fail**, exit 0. Three consecutive full runs by the verifier, plus my own runs. One `[human-verify]` scenario is pending (M0 gate). Disk free about 10 GB after the partial client install (15 GB in `client/`).

## Working conventions
- One git branch per OpenSpec change (`change/<id>`), pushed to the private remote `origin` (github.com/zttimmerman/[redacted]). Each gets a PR, merged to `main` only after a verifier PASS and a green full run, with the verdict in the PR body. `main` = verified work.
- `harness/active-changes` lists the changes being built or verified; only those (plus archived specs) gate `report.coverage` / `report.full_run`.

## Known issues / notes for later milestones
- The playerbots command server (`AiPlayerbot.CommandServerPort`) binds 0.0.0.0 with no address option. It's disabled. If M2/M4 scripting wants it, it needs a module patch to bind 127.0.0.1, and an ADR.
- M0 bot population is capped at 10 random bots (`config.py`). M2 must define the real budget (see research doc §5).
- Probe logout takes about 20 s (logout timer, since the test account isn't GM). Warden packets are ignored, which is fine for short sessions.
- The era-diff tool couldn't read VMaNGOS source to confirm what `RewXP` means, because a web fetch was denied. It's flagged in the memo.

## Backlog (later rounds)
- In-game single-player settings menu: toggle leveling pace/difficulty (vanilla vs WotLK, per character?) without SQL swaps or restarts. Zach's idea from Gate 1.
- Era profile extras descoped from v1: class mount quests, vanilla quest levels, vanilla quest gold, creature damage/armor (needs a researched mapping).
- Separate mount switch (split mounts out of vanilla mode) if wanted.
- WoWSilicon client perf patches (libSiliconPatch etc.): needs §2 opened via an ADR; FPS A/B test first. Tech-debt / M5.
- Verify VMaNGOS `RewXP` semantics against VMaNGOS source (assumed in the memo).

## Correction log
**2026-10-01, target correction from Zach.** The target is WotLK 3.3.5a (build 12340), all content through 80 (Outland, Northrend, DKs). The era profile adjusts 1–60 pacing and difficulty only. VMaNGOS is a read-only data reference. CLAUDE.md §1/§2/§7 updated.

Audit of everything created so far (specs, ADRs, tasks, kickoff split, harness code):
- **CLAUDE.md §1 Goal**: "restores pre-2.3 (vanilla-style) leveling pace" could be read as a vanilla target. **Fixed:** added the explicit Target statement.
- **CLAUDE.md §7 / KICKOFF §8 M3** ("generates the change set from the diff"): taken literally, a VMaNGOS-vs-AC diff covers every shared entry. That would overwrite WotLK-era values (e.g. reworked quests, revamped zones, 3.x class/talent-adjacent data), and it might treat TBC/WotLK entities missing from VMaNGOS as deletions. **Fixed:** the VMaNGOS line went into §7, and a correction note in KICKOFF.md scopes the diff to entities present in both DBs and inside 1–60 scope, additive or override-only, with no deletions. The Gate 1 memo and the diff tool get built to this scope.
- **KICKOFF §3 / §13.4** ("pre-2.3 profile", "VMaNGOS diff approach"): same scoping note applies. No other change.
- **fork.lock** `vmangos_db_url`: it's a data download, not a codebase. **Fixed:** comment added saying it's never built or targeted.
- **M0 specs, design, tasks, ADR-0001/0002/0003, verifier agent, harness code**: no vanilla or VMaNGOS-as-codebase assumption found. All target 3.3.5a build 12340 (client-data v20.0 is 3.3.5a data).

## Open questions for Zach
1. **Client + CrossOver** aren't on this Mac (no `Wow.exe` found, no CrossOver.app). Both are needed for the M0 human gate. Where's your 3.3.5a enUS client? Need the path.
2. **Disk:** ~44 GB free before this project; the build, data, and DB use about 12 GB, and the client needs ~17 GB more. Tight.
3. GitHub fork: the fork lives in-repo as pinned SHA + patch queue (ADR-0001). Want a GitHub remote for this repo? Not needed for progress.

## Environment notes
- Node 20 needed for `openspec` (`~/.nvm/versions/node/v20.20.1/bin`); default shell node is 16.
- Isolated MySQL at 127.0.0.1:3307, datadir `data/mysql`. The Homebrew MySQL service and its datadir are never touched.
