# STATUS

_Last updated: 2026-10-02 (session 2): M1 done_

## Current milestone: M2 Living world (next), plus tooling/tiers/spike
M0 Foundation is **done**: verifier PASS (cycle 4), human gate passed 2026-10-01, archived as `openspec/changes/archive/2026-10-02-m0-foundation-harness`, and its specs merged into `openspec/specs/`.

## Task board
| Task | State | Notes |
|---|---|---|
| M0 Foundation | **done / archived** | 4 verifier cycles (FAILs fixed: playerbots_active weak check; client.inspect ignored unrecorded binaries) |
| Gate 1 | **signed 2026-10-01** | vanilla mode on by default, easily toggleable (memo) |
| M1 `./play` | **done / archived** (verifier PASS cycle 2; Zach's Wi-Fi-off demo passed 2026-10-02) | personal account `PLAYER` (env/player.json), sandbox-exec offline proof, procs.py replaces ps |
| Ghidra tooling + skills | **done / archived** (verifier PASS cycle 2) | `hs re`, `hs ghidra-install`, 4 project skills; run budget: 2 full runs/change |
| F9 re-tooling verifier nits | todo (low) | readonly path check: use resolved `is_relative_to`; JDK assertion `>=21` explicitly; harness check for wine no-adoption rule (needs scratch params like ghidra.install) |
| Harness tiers | specced | branch `change/harness-tiers`, then a builder |
| Login-screen spike | next after tiers | Q1: custom GlueXML from a patch MPQ with an unmodified Wow.exe? (Wow.exe has "GlueXML has corrupt signature" strings). Q2: are the TBC/Vanilla login backdrops and music in the 3.3.5a MPQs (background picker)? Needs an MPQ reader |
| F8 M1 verifier nits | todo (low) | smoke picks `wow[0]` (pick the persistent/oldest pid); `play_processes()` matches any cmdline with 'hslib.play'; real Wow.exe exit path only human-verified |
| M2 living world | spec written | `openspec/changes/m2-living-world` |
| M3 vanilla mode | spec written; task 1.1 done (RewXP verified) | branch `change/m3-vanilla-mode` |
| F2 `report.full_run` proves files written only externally | todo (low) | |
| F5 mtld3d shader cache in `client/`; possible colour banding (HDR on) | todo (low) | |
| F6 setup reproducibility | todo | Brewfile, `hs bootstrap`, `docs/setup.md` |
| F7 verifier-4 nits | todo (low) | smoke's 30 s measured from the launcher, not Wow.exe's own start (Wow.exe ~27 s old at end); stale docstrings (launch_smoke, `_require_wine`); coverage can't see an unlisted change with no ticked tasks (process rule covers it) |

## Last full harness result
2026-10-02, HEAD `48cd8a9`: **27 pass / 0 fail**, exit 0 (mine, plus 2 verifier runs). About 7 min per full run. Disk free about 22 GB.

## North star (Zach, 2026-10-02)
Offline WoW that feels like **one single-player app**, with many moving parts behind the scenes. Target front end: the **in-client modified login screen** (profiles/saves/options, auto-login, backdrop picker), reached via a launcher-side menu first. Client binary changes are expected over time, each with its own ADR and Zach's yes (§2/§11). **If the launcher pre-screen (step 1) feels janky, reconvene with Zach about jumping straight to the exe-patched in-client screen (step 2).**

## Working conventions
- One git branch per OpenSpec change (`change/<id>`), pushed to the private remote `origin` (github.com/zttimmerman/[redacted]). Each gets a PR, merged to `main` only after a verifier PASS and a green full run, with the verdict in the PR body. `main` = verified work.
- `harness/active-changes` lists the changes being built or verified; only those (plus archived specs) gate `report.coverage` / `report.full_run`.

## Known issues / notes for later milestones
- The playerbots command server (`AiPlayerbot.CommandServerPort`) binds 0.0.0.0 with no address option. It's disabled. If M2/M4 scripting wants it, it needs a module patch to bind 127.0.0.1, and an ADR.
- M0 bot population is capped at 10 random bots (`config.py`). M2 must define the real budget (see research doc §5).
- Probe logout takes about 20 s (logout timer, since the test account isn't GM). Warden packets are ignored, which is fine for short sessions.
- The era-diff tool couldn't read VMaNGOS source to confirm what `RewXP` means, because a web fetch was denied. It's flagged in the memo.

## Backlog (later rounds)
- **Front menu** (target state, see North star). Step 1: a launcher-side menu (`.app`: profiles/saves/options, the launcher types the login, backdrop/music swap via a data-only patch MPQ). Step 2: the in-client screen, which needs a Wow.exe signature-check patch on a copy (ADR when we get there). Spike phase A: GlueXML is RSA/MD5-signed and verified each load; backdrops (WotLK/TBC/Dark Portal) and music are present and unsigned. Saves: light (per account) vs true save files (separate DBs), to decide later.
- Mac `.app` launcher for `./play` (right after the spike); optional stopgap where the launcher types the login.
- F6 setup reproducibility: Brewfile, `hs bootstrap`, docs/setup.md.
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
