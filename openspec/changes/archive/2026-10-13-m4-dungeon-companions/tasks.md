Agent-hour estimates for builder work. This change starts after `m4-dungeon-scaling` task 1.1, which proves headless dungeon entry, addclass parties and the selfbot. It can be built after or alongside that change's tasks 2–3, but only one agent owns the stack at a time.

## 1. Prototype (riskiest assumption first)
- [x] 1.1 **Full-run prototype on `spike/m4-dungeon`** (4–5 h): one scripted party clear of The Deadmines with the design.md §4 driver (staging rule, `.mmap path` parse, summon/gather, pulls, wipe recovery), plus one selfbot solo attempt at its first two bosses. Record per encounter: time, wipes, deaths by role, and the GM permissions the companion account lacks. **Stop and report** if the party can't kill VanCleef in 3 attempts; that's the "classic tactics gap" path (design.md §7), a decision for the orchestrator before more building.
  - Done, left unticked until the change is listed as active (`report.coverage` would then require the unbuilt 3.x checks). Result (2026-10-11, `docs/research/m4-full-run.md`): VanCleef died on the first pull in every run that reached him; no tactics gap, so 5.1 is not needed. 4 of 5 runs on the final driver cleared; all wipes were at the first three bosses, mostly while companions were still arriving. The straight-line staging rule failed in 5 of 6 Deadmines bosses and was replaced (design.md §4). The solo selfbot killed the first two bosses. Level-3 account `HSDNGNAV` runs `.mmap path` only.

## 2. Party preset
- 2.1 moved to `m4-party-preset`.
- 2.2 moved to `m4-party-preset`.
- 2.3 moved to `m4-party-preset`.

## 3. Harness and checks
- 3.1 cut 2026-10-12 (spec-change log; built on branch `change/m4-rti-focus`, unmerged). Original: **Probe addition** (1 h): `MSG_RAID_TARGET_UPDATE` send/parse, its format cited to the core's packet builder. (`SMSG_SPELLHEALLOG` and the `co ?` parser dropped with `dng.healer_heals`, spec-change log 2026-10-12.)
- 3.2 moved to `m4-instance-tactics`. Unchanged original text: (3 h), ≥3× each plus counterexamples (a wrong spec in the plan; `ApplyInstanceStrategies = 0`).
- 3.3 cut 2026-10-12 (spec-change log; built on branch `change/m4-rti-focus`, unmerged). Original: **`dng.rti_focus`** (2–3 h) plus a unit-level counterexample (no `rti` order). `dng.tank_holds`, `dng.healer_heals`, `dng.avoid_aoe` and `dng.cc_on_mark` were cut (spec-change log 2026-10-12).
- [x] 3.4 **Run driver hardening plus `dng.party_clear` (4 dungeons), `dng.solo_clear`, `dng.run_budget`** (6–8 h, mostly run time): every run's evidence kept under `env/` (not committed), with a summary table in `docs/research/m4-dungeon-runs.md`.
- 3.5 moved to `m4-party-preset`.

## 4. Docs
- 4.1 moved to `m4-party-preset`.
- [x] 4.2 **design.md "As built"** (0.5 h).

## 5. Contingent (only if 1.1 or 3.4 shows a classic-dungeon tactics gap)
- 5.1 not needed (no tactics gap in 1.1 or 3.4; M4 gate runs 2026-10-12/13). Original: **Upstream search, then pin bump / cherry-pick / `patches/playerbots/0002-…`** with ADR (estimate set at diagnosis; expect 4–8 h).

**Total builder estimate: ~28–34 agent-hours** (excluding 5.1). Plus one verifier session (~2 h, the slow checks run once) and a full run. Then the **M4 human gate** (Zach, ~1.5–2 h of play), after `m4-dungeon-scaling` and `m4-alt-companions` merge.
