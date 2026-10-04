# STATUS

## ▶ Handoff / resume here (rewrite on every material change)
_Updated 2026-10-04 (M3 merged; launcher .app next)._

**Cold start:** read this file, then `openspec list` (Node 20: `export PATH=~/.nvm/versions/node/v20.20.1/bin:$PATH`), then `git -C <repo> worktree list`, `git branch -a`, `gh pr list`, then `harness/hs status`. The skills `.claude/skills/hs-*` hold the procedures (change loop, checks, client debugging, RE). User preferences and decisions are in the orchestrator memory notes (plain language for decisions, Gate 1, front-menu vision, logo).

**In flight**
| Work | Branch / checkout | State | If its agent is gone |
|---|---|---|---|
| In-client login screen (front menu step 2, ADR-0009) | `change/in-client-login` (HEAD 4dafbc9, pushed), **main checkout**, in `harness/active-changes` | B19 done: patch 0001 = 8 bytes in GlueMgr_LoadGlueXML (614e55cc… patched hash), `hs client-build`, front menu (approved design), credentials in a 0600 generated MPQ in the play client only, 9 checks ×3 + 14 counterexamples, quick 42/0, full 76/0. **Fresh verifier running** (owns the stack) | After PASS: archive/PR/merge; Zach plays (human-verify). Disk: ~14 GB free, tight |

**At the M4 human gate, Zach reviews** two performance-budget spec loosenings recorded in the M2 proposal: budget 4.0 GB RSS → 6.0 GB max(RSS, footprint) plus a 7.0 GB stack limit, with host-free made recorded-only (commit 7005613, attributed to Zach); "largest N that fits" → "default fits + larger sizes measured" (ab6447a, my inference from Zach picking ~500). **F18:** playerbots 0001 `Execute` returns false after a successful offline removal (cosmetic, debug stat only). **Screen-recording prompt Zach saw (2026-10-04):** not from our app (the launcher only shows windows and runs ./play). Our only window-list code (`launch.onscreen_windows`) runs in harness checks, not in play. Most likely the Claude app's own request; safe to deny. **Launcher .app DONE (2026-10-04, PR #11):** verifier PASS (full 63/0, app.* 7/7). Zach to double-click `dist/Hearthstone.app` (human-verify; real Wow.exe may trigger other macOS prompts). **F21:** `play.py` EXIT_BUSY=2 collides with argparse's bad-args exit code (app would say "already running"). **F22:** status window flashes before "already running" when ./play is busy; `tcc_prompts()` ignores `since`; `play_processes()` matches any cmdline containing "hslib.play". **M3 DONE (2026-10-04, PR #10):** vanilla mode on by default; two verifier PASSes (22ef40e slow 68/0; 028fecd full 61/0 after B17). Open for Zach: play vanilla mode (human-verify). At the gate: 14442 Hunter Thunderhorn stays stock HP (AC exp=1 → TBC base HP); 6 class-mismatched creatures (2647, 4844, 5647, 6213, 7872, 7874) are ~10–20% off vanilla absolute HP. **F19:** tripwire in `era.scope_guard` if the `exp != 0` exemption count grows beyond era-diff's needs-review count. **F20:** pin the VMaNGOS zip sha256 in fork.lock (today only the URL is pinned; the hash lives in the generated SQL header). **For Zach when playing vanilla mode (from the M3 verifier):** switching vanilla→WotLK lowers XP thresholds, so a character may level up on its next XP gain; the game client may show stale item tooltips after a toggle (cache); over-level quest XP follows VMaNGOS float rounding (e.g. 811 not 810). **M2 DONE (2026-10-03, PR #9):** two verifier PASSes (eca9cb7 slow run 60/0; a01699a re-verify incl. client checks); ADR-0007/0008 accepted. Still open: 3 M2 `[human-verify]` scenarios for Zach's next play session (living world, companions, playability). Verifier-2 notes: Orgrimmar is now the weakest capital (6-8; dips to 4 at the later snapshot); `pop.size_setting` doesn't exercise a population world of another N; `lifecycle.down` passes despite the F15 shutdown segfault. New F-items from B13: **F15** upstream worldserver shutdown segfault (`GameObject::RemoveFromWorld` ← `MapMgr::UnloadAll`, after population sessions; seen before the patches); **F16** checks whisper `reset botAI` because companion strategies persist per bot (note for human-verify: real companions keep their last orders). **F17 (backlog, Zach 2026-10-03):** upstream our fixes (core 0002 → AzerothCore / the playerbots fork, playerbots 0001 → mod-playerbots) as a PR or at least an issue, *only after* reading each repo's contributing guide, issue templates and triage norms, so we don't clutter their trackers. Nothing gets posted without Zach's OK.

**Next (in order):** launcher `.app` (change `launcher-app`) → front menu step 1 → M4 solo dungeons (human gate: Zach plays two dungeons). Zach still to play: M2 + M3 human-verify scenarios.

**Decisions pending from Zach:** none blocking. Later: the patch ADR for the in-client front menu (step 2); light vs true save files; F-items as they come up.

**Compaction hooks (PR #8):** `.claude/settings.json` runs `.claude/hooks/session-context.sh` on compact/clear/resume (injects this handoff + a context-survival checklist) and `precompact-check.sh` before compaction (warns if a change/* branch is newer than this handoff). (The temporary settings.local.json was removed once main was merged into the M2 branch.) Mac sleep: Zach set `pmset disablesleep 1` on 2026-10-03, so a closed lid no longer stalls runs; undo with `sudo pmset -a disablesleep 0`.

**Conventions:** **Before writing any core or module patch, search upstream issues and PRs and check newer versions** (see the hs-change-loop skill); prefer pin bump > cherry-pick > own patch. branch per change, PR + merge after a verifier PASS. Run budget: 2 full runs per change (my pre-check, quick tier OK; verifier: 1 full run, `--force-clean` when build inputs change, + ≥5 targeted repeats), plus a periodic full ×3. **STATUS.md is edited only on `main`** (status-only commits go straight to main; branches don't touch it). Every git write uses `git -C <path>`.


_Last updated: 2026-10-02 (session 2): M1 done_

## Current milestone: launcher `.app` → front menu → M4 solo dungeons
Done: M0 (gate passed), Gate 1 (signed), M1 `./play`, M2 living world (PR #9), M3 vanilla mode (PR #10), plus re-tooling, harness tiers, core server-mail guard, login-screen spike. Zach still to play: M2 + M3 `[human-verify]` scenarios.

## Task board
**Done:** M0, Gate 1, M1, Ghidra tooling, harness tiers, core server-mail guard (ADR-0006), login-screen spike, M2 (ADR-0007/0008), M3. Details in `openspec/changes/archive/` and git history.

**Open follow-ups (F-items), roughly by value:**
| Item | Priority | Notes |
|---|---|---|
| F15 worldserver shutdown segfault (`GameObject::RemoveFromWorld` ← `MapMgr::UnloadAll`) | med | upstream bug, seen after population sessions; `lifecycle.down` passes despite it; check upstream first |
| F14 `hs upstream-check` | med | new commits/issues/PRs in the core fork, AzerothCore and mod-playerbots since our pins; run before any patch |
| F11 server-mail relog misdelivery check | med | second char, fast A→B relog, no misdelivered mail |
| F10 enforce `--force-clean` at merge in the harness; keep the from-empty build log | med | |
| F6 setup reproducibility (Brewfile, `hs bootstrap`, docs/setup.md) | med | prerequisite for the portable app |
| F17 upstream our fixes (PR or issue) | low, needs Zach's OK | only after learning each repo's process |
| F19 tripwire on the `era.scope_guard` `exp != 0` exemption count; F20 pin the VMaNGOS zip sha256 in fork.lock | low | |
| F16 companions keep last orders across logins (checks reset them) | note | human-verify note |
| F18 playerbots 0001 `Execute` returns false after offline removal | low | cosmetic |
| F2, F5, F7, F8, F9, F13 verifier nits | low | see git history of this table for detail |
| F12 keep-awake | done-ish | `caffeinate -s` + Zach's `pmset disablesleep 1` |

## Last full harness result
2026-10-04, M3 re-verify at `028fecd`: unfiltered `--tier full --force-clean` **61 pass / 0 fail** (7 slow skipped by design); last slow run 2026-10-03 at `22ef40e`: **68 / 0**. Post-merge quick tier on main: 37 pass / 1 skip.

## North star (Zach, 2026-10-02)
Offline WoW that feels like **one single-player app**, with many moving parts behind the scenes. Target front end: the **in-client modified login screen** (profiles/saves/options, auto-login, backdrop picker), reached via a launcher-side menu first. Client binary changes are expected over time, each with its own ADR and Zach's yes (§2/§11). **If the launcher pre-screen (step 1) feels janky, reconvene with Zach about jumping straight to the exe-patched in-client screen (step 2).**

## Working conventions
- One git branch per OpenSpec change (`change/<id>`), pushed to the private remote `origin` (github.com/zttimmerman/[redacted]). Each gets a PR, merged to `main` only after a verifier PASS and a green full run, with the verdict in the PR body. `main` = verified work.
- `harness/active-changes` lists the changes being built or verified; only those (plus archived specs) gate `report.coverage` / `report.full_run`.

## Known issues / notes for later milestones
- The playerbots command server (`AiPlayerbot.CommandServerPort`) binds 0.0.0.0 with no address option, so it's disabled. If scripting needs it: a module patch binding 127.0.0.1, plus an ADR.
- Rosetta's `oahd` can wedge (2026-10-03): every untranslated x86_64 binary, Wine included, hangs unkillably in state U. Fix: Zach runs `sudo killall oahd`; reboot is the fallback.
- The app needs macOS Documents-folder permission (repo is in ~/Documents); granted 2026-10-04 for `local.hearthstone.app`. Don't change the bundle id or designated requirement, or macOS re-prompts.

## Roadmap and backlog (groomed 2026-10-04)
**Now:** in-client login screen (verifying) → **launcher polish** (change `launcher-polish`; ADR-0010 Wine host shim accepted by Zach 2026-10-04, option A: one Dock icon; loading screen with stages, launches when the world is ready and shows the bot count; builds after the login verifier frees the stack) → **M4 solo dungeons** (human gate: Zach plays two dungeons; review M2 budget loosenings there).

**M5 (after M4), proposed order:**
1. **Solo-first feature audit (Zach, 2026-10-04).** Principle: every MMO feature either serves solo play or gets hidden. Hide: global chat channels, trade/LFG/guild-recruitment noise, anything waiting on strangers. Keep: visible bots, instant groups, useful systems. Mostly config/module work.
2. **(Moved up: now before M4, in flight.)** **In-client unified login screen (front menu step 2), fast follow (Zach, 2026-10-04).** Profiles/saves/options/auto-login/backdrop picker inside the game's own login screen, built on a revertable derived "play client" (`env/client-play/`, APFS clone of the pristine `client/` + `patches/client/` queue). **ADR-0009 accepted (Zach, 2026-10-04).** Given this, front menu step 1 stays thin: the profile/account/options/auto-login backend that step 2 reuses, with minimal launcher UI.
3. **Auction house with real listings.** Evaluate first: does mod-playerbots' random-bot economy post to the AH at all, and AzerothCore's AH-bot module (seeds listings from item data, can buy players' items). Pick the less invasive one; listings priced from data, not invented.
4. **Transmog spike.** `azerothcore/mod-transmog` (AGPL-3.0). Target as modern-retail as possible: account-wide appearance collection, outfits saved independently of items (Midnight-style). Evaluate what the module gives vs needs building; client UI limits.
5. **LLM companions spike (Zach, 2026-10-04).** Local model (e.g. Ollama), toggleable, off the main path, time-boxed. Use it for companion orders in plain language, personality/banter, lore comments: a few calls a minute, not the 500-bot population. Measure memory/latency next to the server + Wine. Goal: companions *better than* human party members, not fake strangers. M4 companions come first.
6. **In-game single-player settings menu** (Gate 1 idea): toggle pacing/difficulty without SQL swaps or restarts; may fold into the front menu step 2 (in-client login screen, needs a Wow.exe patch ADR + Zach's yes).
7. **Player-centred population** (busier around the player, no pop-in; details in git history of this section).
8. **Portable packages: "write once, build per OS, play anywhere" (Zach, 2026-10-04).** Development stays on the Mac; the *packaged game* runs on macOS and Windows. A deterministic `hs package --target macos|windows` (+ a skill) built from `main`:
   - **Shared, built once:** game data and DBs, module SQL, configs, client files, the client patch queue (same Wow.exe on both), launcher logic where possible.
   - **Per OS (native code):** the AzerothCore server + modules (macOS arm64 here; Windows x64 needs a Windows build host: a GitHub Actions Windows runner or a Windows VM, decide later), MySQL runtime, launcher. macOS wraps Wow.exe in the Wine/Metal runtime; Windows runs it natively.
   - **Saves outside the package** (characters + auth DBs, client WTF settings) in the OS's app-data folder, with first-run DB init/migrate; same schema on both, so a save can be exported/imported between Mac and Windows.
   - **Packages ship complete, client included (Zach, 2026-10-04):** one self-contained package per OS, prepackaged client inside; copying it between Zach's own machines is personal use, not redistribution.
   - macOS: self-contained `.app`, ad-hoc signed (another Mac: right-click Open + TCC prompts). Windows: a folder/zip with a launcher and shortcut (SmartScreen warning on first run, unsigned). Tens of GB each; personal use only, never redistributed (CLAUDE.md §1).
   - Checks: reproducible packages; each runs from a fresh folder with the repo absent; an upgrade keeps an existing save; save export/import round-trip. Windows checks need a Windows runner. Needs F6 first.
9. **Releases (Zach, 2026-10-04), after item 8.** Cut versioned releases when `main` updates, manually first, then automatically, gated on a green harness for that commit (verifier PASS + full run). **One complete package per OS, client included.**
   - **Phase 1 (Zach, 2026-10-04): GitHub Releases + bring-your-own-client.** Releases carry only our artifacts (split into <2 GiB parts if needed). The client is supplied once, at first setup, from a local copy; Zach's own machines keep `hs package` for complete client-included packages to move between machines. Reason: GitHub's terms bar hosting copyrighted files you don't own, even in private repos (account risk). **Target state (agreed):** full packages stored on the [redacted] [redacted] (share or MinIO), GitHub Release holds version/changelog/checksums/link.
   - **Runners:** Windows x64 builds in a Windows VM on the [redacted] box as a self-hosted GitHub Actions runner (if it's x86_64); Linux jobs in a container there. **macOS builds stay on the Mac** (self-hosted runner on the Mac): macOS VMs on non-Apple hardware break Apple's licence, and our arm64 build needs Apple Silicon. Self-hosted runners are OK only because the repo is private and only Zach pushes; keep the runner VMs isolated.
   - Version tags + a changelog from merged PRs/archived OpenSpec changes; each release records the commit, the harness result and the pins (fork.lock). Updates could ship as deltas later (only changed parts), the way game launchers do.

**Curated addon pack (Zach, 2026-10-04), candidate for M5 after the in-client login screen.** Ship popular addons as a pinned, toggleable plugin layer, not baked into the game's own UI code. 3.3.5a loads addons from `Interface/AddOns` without signature checks (only GlueXML/FrameXML core is signed), so this is a data-only addition to the play client (ADR-0009), no binary patch. Use 3.3.5a-compatible backports (modern versions use newer APIs): candidates Bagnon, Pawn, Questie (3.3.5 port; useful with vanilla mode), AtlasLoot, a damage meter (Details/Recount), DBM for M4 dungeons, and a mod-playerbots control UI addon for companions (verify which exist and work). Each pinned by source + hash, licence recorded (personal use, private repo), enabled per profile from the front menu. Checks: each loads with zero Lua errors in a scripted client session; disabling the pack gives a clean UI. Making them "native" (merged into FrameXML) is rejected: it needs another signature patch and loses easy updates.

**Launcher polish (Zach play test, 2026-10-04):** (1) one Dock icon: today the Hearthstone launcher and Wine's Wow.exe both show; clicking Hearthstone says "already running" while the Wine icon focuses the game. Target: Hearthstone is the only visible app (e.g. the launcher activates/focuses the game window on reopen, and Wine's icon is hidden or carries the Hearthstone icon); spike what Wine's macdrv allows. (2) loading screen with a progress indicator and the current stage ("starting database", "loading world", "spawning 500 bots", "launching game"), like Guild Wars 2's launcher. Zach liked the current "please wait" text.

**Retail-style shared tagging (Zach play test, 2026-10-04):** in 3.3.5 a mob belongs to whoever hits it first, so at startup the bots sweep starting-zone quest mobs and the player has to wait. Target: retail behaviour, where any same-faction player who damages a mob gets kill/quest credit (and loot eligibility), for all mobs, not just elites. Check upstream modules/issues first (AzerothCore, mod-playerbots); likely a module using kill/credit hooks; a cheaper interim could be fewer or staggered bots in starting zones at startup.

**Front-menu follow-ups (Zach, 2026-10-04):** a setting to swap the WoW logo on the login screen; creating profiles from the menu; world size + vanilla mode in Settings (next change).

**Later / tech debt:** era extras (class mount quests, vanilla quest levels and gold, creature damage/armor, separate mount switch); WoWSilicon client perf patches (needs a §2 ADR, FPS A/B first); the F-items above.

## Correction log
**2026-10-01, target correction from Zach.** The target is WotLK 3.3.5a (build 12340), all content through 80 (Outland, Northrend, DKs). The era profile adjusts 1–60 pacing and difficulty only. VMaNGOS is a read-only data reference. CLAUDE.md §1/§2/§7 updated.

Audit of everything created so far (specs, ADRs, tasks, kickoff split, harness code):
- **CLAUDE.md §1 Goal**: "restores pre-2.3 (vanilla-style) leveling pace" could be read as a vanilla target. **Fixed:** added the explicit Target statement.
- **CLAUDE.md §7 / KICKOFF §8 M3** ("generates the change set from the diff"): taken literally, a VMaNGOS-vs-AC diff covers every shared entry. That would overwrite WotLK-era values (e.g. reworked quests, revamped zones, 3.x class/talent-adjacent data), and it might treat TBC/WotLK entities missing from VMaNGOS as deletions. **Fixed:** the VMaNGOS line went into §7, and a correction note in KICKOFF.md scopes the diff to entities present in both DBs and inside 1–60 scope, additive or override-only, with no deletions. The Gate 1 memo and the diff tool get built to this scope.
- **KICKOFF §3 / §13.4** ("pre-2.3 profile", "VMaNGOS diff approach"): same scoping note applies. No other change.
- **fork.lock** `vmangos_db_url`: it's a data download, not a codebase. **Fixed:** comment added saying it's never built or targeted.
- **M0 specs, design, tasks, ADR-0001/0002/0003, verifier agent, harness code**: no vanilla or VMaNGOS-as-codebase assumption found. All target 3.3.5a build 12340 (client-data v20.0 is 3.3.5a data).

## Open questions for Zach
When convenient: play M2 + M3 (living world, companions, smoothness, vanilla pacing/elites); first double-click of `Hearthstone.app` once its verifier passes.

## Environment notes
- Node 20 needed for `openspec` (`~/.nvm/versions/node/v20.20.1/bin`); default shell node is 16.
- Isolated MySQL at 127.0.0.1:3307, datadir `data/mysql`. The Homebrew MySQL service and its datadir are never touched.
