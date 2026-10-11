Agent-hour estimates for builder work. This change starts after `m4-dungeon-scaling` task 1.1, which proves headless dungeon entry, addclass parties and the selfbot. It can be built after or alongside that change's tasks 2–3, but only one agent owns the stack at a time.

## 1. Prototype (riskiest assumption first)
- [ ] 1.1 **Full-run prototype on `spike/m4-dungeon`** (4–5 h): one scripted party clear of The Deadmines with the design.md §4 driver (staging rule, `.mmap path` parse, summon/gather, pulls, wipe recovery), plus one selfbot solo attempt at its first two bosses. Record per encounter: time, wipes, deaths by role, and the GM permissions the companion account lacks. **Stop and report** if the party can't kill VanCleef in 3 attempts; that's the "classic tactics gap" path (design.md §7), a decision for the orchestrator before more building.

## 2. Party preset
- [x] 2.1 (moved to `m4-party-preset`) **`MoongladeParty.lua` (`/mg party`, `/mg party clear`)** (3 h): `MG_PARTY_PLAN` table, role detection from talent points, addclass/whisper sequence, hunter pet stance.
- [x] 2.2 (moved to `m4-party-preset`) **Harness Lua-table reader, plus plan validation inside `dng.party_preset`** (1 h): spec names exist in the pinned `playerbots.conf.dist`, and classes are valid addclass words (`PlayerbotMgr.cpp:1083-1130`).
- [x] 2.3 (moved to `m4-party-preset`) **Config** (0.5 h): explicit `AutoAvoidAoe`/`ApplyInstanceStrategies` with a comment; profile `aoe-avoid-off` for the counterexample.

## 3. Harness and checks
- [ ] 3.1 **Probe additions** (2 h): `SMSG_SPELLHEALLOG`, `MSG_RAID_TARGET_UPDATE` send/parse, `co ?` reply parser, each format cited to the core's packet builder.
- [ ] 3.2 **`dng.instance_strategy`** (the party checks moved to `m4-party-preset`) (3 h), ≥3× each plus counterexamples (a wrong spec in the plan; `ApplyInstanceStrategies = 0`).
- [ ] 3.3 **`dng.tank_holds`, `dng.healer_heals`, `dng.avoid_aoe`, `dng.rti_focus`, `dng.cc_on_mark`** (5–6 h), ≥3× each plus counterexamples (no tank; no healer; `aoe-avoid-off`; no `rti` order; no `rti cc`). Tune `wait for attack time` here and record why.
- [ ] 3.4 **Run driver hardening plus `dng.party_clear` (4 dungeons), `dng.solo_clear`, `dng.run_budget`** (6–8 h, mostly run time): every run's evidence kept under `env/` (not committed), with a summary table in `docs/research/m4-dungeon-runs.md`.
- [x] 3.5 (moved to `m4-party-preset`) **`client.party_slash` GUI check** (2 h): real client, harness test character, never PLAYER; GUI path rule updated for `MoongladeParty.lua`.

## 4. Docs
- [x] 4.1 (moved to `m4-party-preset`) **`docs/companions-in-dungeons.md` cheat sheet** (1 h): `/mg party`, orders (`rti skull`, `rti cc moon`, `tank attack`, `pull`, `stay`, `follow`, `flee`, `focus heal`), what companions do on their own, and known limits (no classic boss tactics; trash walk is real play).
- [ ] 4.2 **design.md "As built"** (0.5 h).

## 5. Contingent (only if 1.1 or 3.4 shows a classic-dungeon tactics gap)
- [ ] 5.1 **Upstream search, then pin bump / cherry-pick / `patches/playerbots/0002-…`** with ADR (estimate set at diagnosis; expect 4–8 h).

**Total builder estimate: ~28–34 agent-hours** (excluding 5.1). Plus one verifier session (~2 h, the slow checks run once) and a full run. Then the **M4 human gate** (Zach, ~1.5–2 h of play), after `m4-dungeon-scaling` and `m4-alt-companions` merge.
