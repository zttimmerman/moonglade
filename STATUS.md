# STATUS

## ▶ Handoff / resume here (rewrite on every material change)
_Updated 2026-10-03 (after B13; M2 verifier running)._

**Cold start:** read this file, then `openspec list` (Node 20: `export PATH=~/.nvm/versions/node/v20.20.1/bin:$PATH`), then `git -C <repo> worktree list`, `git branch -a`, `gh pr list`, then `harness/hs status`. The skills `.claude/skills/hs-*` hold the procedures (change loop, checks, client debugging, RE). User preferences and decisions are in the orchestrator memory notes (plain language for decisions, Gate 1, front-menu vision, logo).

**In flight**
| Work | Branch / checkout | State | If its agent is gone |
|---|---|---|---|
| M2 living world | `change/m2-living-world`, **main checkout** | **Verifier PASS** at eca9cb7 (all 12 automated scenarios, 1 slow run + 5× comp/core repeats + 2 extra pop sessions; logs /tmp/verif/). Its concerns are being fixed by **builder B14** (owns the stack): (1) `core.login_logout_race_baseline` took its defect frames from a stale coalesced crash report in 6/6 runs, so it must capture this run's backtrace; (2) Undercity capital margin is thin (min 3 once), so raise its weight; (3) `pop.size_setting` should go through ./play's path, and ./play must not silently keep a wrong-size running world; (4) `comp.assists` should summon before the fight | After B14: a targeted fresh verifier on those 4 items (+ quick tier), then archive/PR/merge, ADR-0007/0008 accepted |

**At the M4 human gate, Zach reviews** two performance-budget spec loosenings recorded in the M2 proposal: budget 4.0 GB RSS → 6.0 GB max(RSS, footprint) plus a 7.0 GB stack limit, with host-free made recorded-only (commit 7005613, attributed to Zach); "largest N that fits" → "default fits + larger sizes measured" (ab6447a, my inference from Zach picking ~500). **F18:** playerbots 0001 `Execute` returns false after a successful offline removal (cosmetic, debug stat only). **After merge:** archive m2, PR + merge, set ADR-0007/0008 to accepted. New F-items from B13: **F15** upstream worldserver shutdown segfault (`GameObject::RemoveFromWorld` ← `MapMgr::UnloadAll`, after population sessions; seen before the patches); **F16** checks whisper `reset botAI` because companion strategies persist per bot (note for human-verify: real companions keep their last orders). **F17 (backlog, Zach 2026-10-03):** upstream our fixes (core 0002 → AzerothCore / the playerbots fork, playerbots 0001 → mod-playerbots) as a PR or at least an issue, *only after* reading each repo's contributing guide, issue templates and triage norms, so we don't clutter their trackers. Nothing gets posted without Zach's OK.

**Next (in order):** M2 finish + verify → M3 vanilla mode (`openspec/changes/m3-vanilla-mode`, task 1.1 done on `change/m3-vanilla-mode`, rebase before continuing) → launcher `.app` (logo source at `env/branding/logo-source.jpeg`) → front menu.

**Decisions pending from Zach:** none blocking. Later: the patch ADR for the in-client front menu (step 2); light vs true save files; F-items as they come up.

**Compaction hooks (PR #8):** `.claude/settings.json` runs `.claude/hooks/session-context.sh` on compact/clear/resume (injects this handoff + a context-survival checklist) and `precompact-check.sh` before compaction (warns if a change/* branch is newer than this handoff). (The temporary settings.local.json was removed once main was merged into the M2 branch.) Mac sleep: Zach set `pmset disablesleep 1` on 2026-10-03, so a closed lid no longer stalls runs; undo with `sudo pmset -a disablesleep 0`.

**Conventions:** **Before writing any core or module patch, search upstream issues and PRs and check newer versions** (see the hs-change-loop skill); prefer pin bump > cherry-pick > own patch. branch per change, PR + merge after a verifier PASS. Run budget: 2 full runs per change (my pre-check, quick tier OK; verifier: 1 full run, `--force-clean` when build inputs change, + ≥5 targeted repeats), plus a periodic full ×3. **STATUS.md is edited only on `main`** (status-only commits go straight to main; branches don't touch it). Every git write uses `git -C <path>`.


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
| Harness tiers | **done / archived** (verifier PASS) | `hs run --tier quick` ~36 s (builders' loop); full ~9.5 min; `--force-clean` ~13 min (required once per merge); test logout ~20 s → <1 s |
| F10 tiers verifier nits | todo | enforce force-clean at merge in the harness (e.g. a merge-gate flag/check), not just the verifier brief; keep the from-empty build log (don't let `build.clean_smart` overwrite it) |
| Core server-mail guard (ADR-0006) | **done / merged** (PR #6) | first core patch; M2 unblocked |
| F14 `hs upstream-check` | todo | list new commits, issues and PRs in the core fork, upstream AzerothCore and mod-playerbots since our `fork.lock` pins (esp. fixes for our patches/known issues). Run periodically and before writing any patch |
| F13 MPQ guard hard-link edge | todo (low) | refuse an existing destination with link count > 1; NFD fallback for a missing non-ASCII root (unreachable today) |
| F12 keep the Mac awake during harness runs | partly in M2 (B13 adds `caffeinate -s`); **a closed lid still sleeps the Mac: keep the lid open during long runs** | a mysqld abort ("Semaphore wait > 600 s") happened while the display slept during spike Phase B; B11's later runs under `caffeinate -d -i -u` were clean. Wrap `hs run`/`./play`/long builder runs in caffeinate (or `IOPMAssertion`); add a check |
| F11 server-mail relog misdelivery check | todo | behavioural check: second char on test account, A→B fast relog, no misdelivered mail (requirement narrowed in core-server-mail-guard, logged) |
| Login-screen spike | **done / merged** (PR #7) | report: docs/research/login-screen-spike.md |
| Login-screen spike (old row) | superseded | Q1: custom GlueXML from a patch MPQ with an unmodified Wow.exe? (Wow.exe has "GlueXML has corrupt signature" strings). Q2: are the TBC/Vanilla login backdrops and music in the 3.3.5a MPQs (background picker)? Needs an MPQ reader |
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
- **Upstream core crash (found by B9, 2026-10-02):** a logout within milliseconds of login segfaults worldserver. `src/server/scripts/World/server_mail.cpp:29` `OnPlayerLogin` queues async SELECTs whose callbacks call `SendServerMail(session->GetPlayer(), …)` (line 58) with no null check. The harness works around it (`fixtures.settle_login`). **M2 prerequisite:** an ADR plus a minimal core patch (null-guard) before scaling the bot population, since bots log in and out often.
- The playerbots command server (`AiPlayerbot.CommandServerPort`) binds 0.0.0.0 with no address option. It's disabled. If M2/M4 scripting wants it, it needs a module patch to bind 127.0.0.1, and an ADR.
- M0 bot population is capped at 10 random bots (`config.py`). M2 must define the real budget (see research doc §5).
- Probe logout takes about 20 s (logout timer, since the test account isn't GM). Warden packets are ignored, which is fine for short sessions.
- The era-diff tool couldn't read VMaNGOS source to confirm what `RewXP` means, because a web fetch was denied. It's flagged in the memo.

## Backlog (later rounds)
- **Player-centred population spike (Zach, 2026-10-03):** make the player's zone feel busier without pop-in. Idea: keep the world-wide population, but bias bot placement toward the player's level band and current zone, lower the update rate of bots far from the player (verify which mod-playerbots settings do this, e.g. the "active alone" scaling, before relying on them), and possibly raise the total N using the measured headroom (N=1500 fit). Rejected: bots exist only in the player's zone (pop-in, no living world, empty starting zones/capitals). Zone liveliness is [human-verify]. Pairs with the world-size option in the front menu.
- **Transmog spike** (Zach, 2026-10-03): evaluate `azerothcore/mod-transmog` (AGPL-3.0, Rochet2-based; catalogue id 74011079). **Target = as modern-retail as possible:** account-wide appearance collection, and **outfits saved independently of items (Midnight-style)**, applied on top of current gear. Questions: what the module does vs the target; configure/extend vs our own module (ADR if core); a retail-like wardrobe UI via a normal AddOn + addon messages (confirm with read-only RE that AddOns aren't blocked by the FrameXML/AddOn signature path); interplay with bots and vanilla mode. Don't assume module features; establish them.
- **Front menu** (target state, see North star). Options include world size (population presets). Step 1: a launcher-side menu (`.app`: profiles/saves/options, the launcher types the login, backdrop/music swap via a data-only patch MPQ). Step 2: the in-client screen, which needs a Wow.exe signature-check patch on a copy (ADR when we get there). Spike phase A: GlueXML is RSA/MD5-signed and verified each load; backdrops (WotLK/TBC/Dark Portal) and music are present and unsigned. Saves: light (per account) vs true save files (separate DBs), to decide later.
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
