# STATUS

_Last updated: 2026-10-01 (session 1)_

## Current milestone: M0 Foundation
Change: `openspec/changes/m0-foundation-harness/` (proposal, 5 spec deltas, design, tasks).

## Task board
| Task | State | Notes |
|---|---|---|
| 1.1 Repo structure, fork.lock, ADRs | done | ADR-0001..0003 |
| 1.2 harness skeleton + runner | in progress | `harness/hs`, `hslib/` |
| 2.1 `hs build` | in progress | first native build running (MySQL 9.2 + Boost 1.92 configured OK) |
| 3.x lifecycle | in progress | `hslib/stack.py` written, untested |
| 4.x GM/SQL | in progress | `soap.py`, `db.py` written, untested |
| 5.x protocol probe + fixtures | todo | builder B1 |
| 6.1 client-config | todo | builder B2 |
| 7.1 reporting completeness | todo | |
| 8.1 playerbots scripting research | todo | builder B3, non-blocking |
| 9.x verifier pass, human gate | todo | |
| Gate 1 memo (era profile) | todo | builder B4; scoped per correction below |

## Last full harness result
None yet.

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
