# M4 companions: party clears of the listed dungeons

Change `m4-dungeon-companions`, task 3.4, 2026-10-12, branch `change/m4-party-clear-2`. The gate check `dng.party_clear` (driver `dungeon.run_party`, design.md §4) now runs all four listed dungeons one after another: The Deadmines, Razorfen Downs, Hellfire Ramparts and Utgarde Keep (normal). The Deadmines slice and the prototype are in `docs/research/m4-full-run.md`.

## Method
- **Facts by rule** (`dungeon.facts`, `m4-dungeon-scaling` design.md §6): map, `LFGDungeons.dbc` `Target_Level`, entrance (`areatrigger_teleport`), encounters by `DungeonEncounter.dbc` orderIndex. Nothing per dungeon is typed in except the four LFG ids.
- **Required encounters:** a static spawn of the credit entry, plus always the final encounter. Event encounters that aren't final are reported "skipped: no static spawn".
- **Party:** the probe (human warrior at the target level, `initself=rare`) fights as the fifth member through the playerbots self-AI. The companions are the `/mg party` damage-dealer plan: prot warrior, holy priest, frost mage, combat rogue. AutoBalance counts 5 players, so world health multiplier = 1.0.
- **Instance tactics:** playerbots applies `tbc-ramp` (543) and `wotlk-uk` (574) itself on entry (`ApplyInstanceStrategies`).
- **Builder runs:** each dungeon ran alone with `HS_DNG_PARTY_ONLY=<LFG id>`. Such a run reports SKIP ("partial run … not a gate pass") when everything cleared, so it is never a gate pass. The full four-dungeon run is the milestone gate's.
- **Time:** "Total" is `run_party`'s clock, from the unbind before entry to the last encounter (the 45-minute limit). "Session" adds the nav pass, the login and the party setup.
- **Evidence:** `harness/out/m4-full-run/party-<time>.json` and `party.log` (not committed).

## Summary
| Dungeon (LFG, map, level) | Run | Cleared | Wipes per boss | Skipped encounters | Total | Session |
|---|---|---|---|---|---|---|
| Razorfen Downs (20, 129, 36) | 1 | yes, mask 14 | none | 434 (Tuten'kash, gong event) | 339 s | 400 s |
| Razorfen Downs | 2 | yes, mask 14 | none | 434 | 357 s | ~390 s |
| Hellfire Ramparts (136, 543, 63) | 1 | yes, mask 7 | none | none (the final, Vazruden, is event-summoned and required) | 228 s | 300 s |
| Hellfire Ramparts | 2 | yes, mask 7 | none | none | 277 s | ~380 s |
| Utgarde Keep (202, 574, 72) | 1 | **no**: Prince Keleseth, 2 timed-out pulls and 1 wipe | Keleseth 1 (+2 timeouts) | none | 969 s | 1033 s |
| Utgarde Keep | 2 (staging retry) | yes, mask 7 | none | none | 312 s | 400 s |

Ramparts run 2 and Razorfen Downs run 2 ran in one session (`HS_DNG_PARTY_ONLY=20,136`, 769 s). Runs 1 of Razorfen Downs and Ramparts ran before the Utgarde Keep staging retry. Neither dungeon reaches that rule: their fallback paths are all usable (type 1). A nav pass for The Deadmines after the change gave the same six fallback verdicts as before.

## Per encounter
Format: scaled max health / pulls / wipes / deaths by role / fight time / time including the gather / boss engaged before the order.

| Boss | Run 1 | Run 2 |
|---|---|---|
| Mordresh Fire Eye | 7760 / 1 / 0 / — / 31.5 s / 34.1 s / no | 7760 / 1 / 0 / — / 26.4 s / 29.0 s / no |
| Glutton | 10687 / 1 / 0 / — / 37.1 s / 37.1 s / yes | 10687 / 1 / 0 / — / 43.2 s / 43.2 s / yes |
| Amnennar the Coldbringer | 8743 / 1 / 0 / — / 36.0 s / 38.6 s / no | 8743 / 1 / 0 / — / 36.0 s / 42.3 s / no |
| Watchkeeper Gargolmar | 33380 / 1 / 0 / damage 1 / 37.9 s / 37.9 s / yes | 33380 / 1 / 0 / — / 57.1 s / 57.1 s / yes |
| Omor the Unscarred | 59833 / 1 / 0 / — / 36.0 s / 38.6 s / no | 59833 / 1 / 0 / — / 34.0 s / 36.7 s / no |
| Vazruden (event; staged on Vazruden the Herald) | 19227 (Vazruden, read by GUID in the fight) / 1 / 0 / — / 22.1 s / 24.7 s / no | 19227 / 1 / 0 / — / 24.1 s / 26.7 s / no |
| Prince Keleseth | 192189 / 3 / 1 + 2 timeouts / tank 1, healer 1, damage 2, probe 1 / 840 s / 859 s / no | 192189 / 1 / 0 / — / 97.9 s / 107.7 s / no |
| Dalronn the Controller | not reached | 96094 / 1 / 0 / — / 39.0 s / 45.2 s / no |
| Ingvar the Plunderer | not reached | 120118 / 1 / 0 / probe 1, damage 1 / 98.4 s / 104.6 s / no |

## Findings
1. **Hellfire Ramparts' final boss is event-summoned.** Vazruden (17537, encounter 396, `lastEncounterDungeon` 136) has no static spawn. The required-encounter rule would have skipped it, but the spec says the final encounter is always required.
   - The driver now stages Vazruden on its summoner's static spawn (Vazruden the Herald, the `scale.solo_final_boss` rule). The fight starts at the sentries' summon point from the core script (`boss_vazruden_the_herald.cpp` Reset).
   - The herald flies, and its `.mmap path` ended in the air (z 100 over a floor at 83). So the nav pass paths from a summoned Hellfire Sentry (`NPC_HELLFIRE_SENTRY`, `hellfire_ramparts.h`) instead.
   - The pull is the probe stepping onto the summon point with GM mode off, with no tank order, since the herald is passive and out of reach.
   - Vazruden's scaled health is read by GUID once it shows up in combat.
   - Both runs set bit 2 about 22–24 s after the step-in. The kill credit needs Vazruden (17537) to die, so the bit is the evidence.
2. **Utgarde Keep run 1 failed at Prince Keleseth on staging, not tactics.** The boss's `.mmap path` from the entrance has no navmesh route (type 10, shortcut plus no path). Its "points" were the straight line, so the fallback staging point (189, 168, z 37.9) sat under the room floor (z 40.8), off the navmesh.
   - Pulls 1–2: the tank never engaged (no path, no line of sight; the approach list was empty). Each pull timed out after 300 s with the boss untouched.
   - Pull 3: the party fought something but not Keleseth (6530 damage to it). Everyone died over 240 s: the healer first, then the tank, the probe and the damage dealers. Keleseth was never below full health.
   - Fix (design.md §4 "As built"): a path with the no-path bit is never a walk. The retry tries points at the boss's own height, on the line and then turned around the boss, and keeps the first with a usable path within the walk limits.
   - Run 2 then staged all three bosses (Keleseth: 30 yd turned 45°; Dalronn: 30 yd on the line; Ingvar: the line's point, checked against its own path) and cleared on first pulls.
   - Keleseth also walks around his room: the live reads put him 10–20 yd from his spawn.
3. **No tactics gap in the three new dungeons.** Every boss died on its first pull in the runs with working staging. There were no wipes, and deaths were rare (a damage dealer at Gargolmar; the probe and the rogue at Ingvar). Contingent task 5.1 is not triggered.
4. **Early engagement** (the boss in a fight before the order) happened at Glutton and Gargolmar in both runs. Neither pull was flagged unready.
5. **Budget:** the four dungeons' run clocks are about 15–20 min (The Deadmines) + 6 + 4–5 + 5 min. With the nav passes and the four logins, a gate run of `dng.party_clear` should take about 40–45 min of wall time. That's inside the design's 60–90 min slow-tier estimate for the companion run checks.

## Not done here
- No four-dungeon run end to end (the milestone gate does that).
- No new Deadmines run in this slice: its driver path is unchanged, and the nav pass gave identical verdicts.
- Only 2 runs per new dungeon (the brief's cap), not 3.
- `dng.solo_clear` and `dng.run_budget` aren't built yet (built in the next section).

## Solo clear and the performance budget (2026-10-12)

Change `m4-dungeon-companions`, task 3.4, last slice, branch `change/m4-solo-budget`. Two gate checks, both on The Deadmines (LFG 6, map 36, level 19):
- **`dng.solo_clear`** replaces the prototype `dng.solo_two_deadmines`. The probe runs alone, driven by its self-AI (`.playerbots bot self`), with `initself=rare`, through `dungeon.run_party` in mode "solo", against every required encounter. AutoBalance counts 1 player, so the world health multiplier is 0.167.
- **`dng.run_budget`** is the same party run as `dng.party_clear`'s Deadmines run (`party_checks.party_dungeon`), under the population profile at its default size (N = 500). It waits for the warm-up first: the worldserver was restarted with that profile 15–16 s before the check, so it waited 554–555 s. Then it samples every 10 s from `run_party`'s start to its end. It can't share `dng.party_clear`'s run, because that check uses the 10-bot `scaling-on` profile.
- **Builder runs:** `hs run --only dng.solo_clear --only dng.run_budget`, twice. Each invocation restarts the worldserver once per profile.
- **Evidence:** `harness/out/m4-full-run/solo-<time>.json`, `budget-<time>.json` (samples included), `solo.log`, `budget.log` (not committed).

### Solo
| Run | Result | Deaths per boss | Fight time per boss (Rhahk'Zor, Gilnid, Mr. Smite, Cookie, Greenskin, VanCleef) | Total (`run_party` clock) | Check |
|---|---|---|---|---|---|
| 1 | cleared, mask 125 | 0 at every boss | 12.5, 24.0, 53.3, 36.6, 57.6, 15.7 s | 241 s | PASS (338 s) |
| 2 | cleared, mask 125 | 0 at every boss | 12.5, 30.2, 55.4, 45.9, 38.7, 9.4 s | 234 s | PASS (330 s) |

Every boss died on its first pull. The solo-scaled max health was Rhahk'Zor 751, Gilnid 486, Mr. Smite 648, Cookie 486, Greenskin 486 and VanCleef 648. Every pull's `.gm off` answered "GM mode is OFF".

### Budget (population N = 500)
Limits: worldserver ≤ 6.0 GB, mysqld ≤ 1.0 GB, all three ≤ 7.0 GB (max(RSS, footprint)); world-update p95 ≤ 100 ms, median ≤ 50 ms (`.server info`, per sample).

| Run | Party clear | Samples | Peak worldserver | Peak mysqld | Peak authserver | Peak total | Update p95 (median / max over samples) | Update median (median / max) | Host free+inactive (min) | Check |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | cleared, 0 wipes, 766 s | 78 | 5.186 GB | 0.763 GB | 0.013 GB | 5.961 GB | 6 / 12 ms | 2 / 2 ms | 2.62 GB | FAIL: coverage rule defect (see below); every limit held |
| 2 | cleared, 0 wipes (damage dealers died once each at Gilnid and Mr. Smite), 851 s | 87 | 5.168 GB | 0.762 GB | 0.014 GB | 5.943 GB | 7 / 10 ms | 2 / 2 ms | 2.88 GB | PASS (1513 s) |

### Findings
1. **The solo self-AI clears The Deadmines with room to spare:** about 4 minutes, no deaths. With the 0.167 multiplier every boss has under 800 HP. The party run is the harder fight (multiplier 1.0, about 13–14 min).
2. **Run 1's budget failure was a bug in the check, not the stack.** A sample's time is stamped after its readings. The first sample, taken as the run started, carried a time 0.1 s after `run_party`'s start and failed the "at or before the start" rule. The rule now allows one sampling interval at each end. Re-judged under the fixed rule, run 1's own samples pass. Changing one of them (p95 101 ms, or worldserver 6.01 GB) fails it, and so does an empty sample list.
3. **Headroom:** memory dominates. Worldserver peaked at 5.19 GB against the 6.0 GB limit. Update times stayed far under the limits (p95 at most 12 ms).
4. **Wall time:** `dng.run_budget` takes about 25 min, made up of the worldserver restart, the 9.5-minute warm-up, the nav pass and the 13–14-minute clear. When the `perf.*` checks run in the same full run, they warm the world first and the wait is skipped. `dng.solo_clear` takes about 5.5 min.

## M4 milestone gate (2026-10-12/13)
Three unfiltered `hs run --tier full` runs. Each failure was fixed before the next run.

| Run | `dng.party_clear` | `dng.solo_clear` | `dng.run_budget` | Other fails |
|---|---|---|---|---|
| 1 (main c88bffd) | FAIL: Mr. Smite pulled with 3 dead after a Gilnid timeout (fixed #21: a gather that runs out is held, revive only when quiet) | PASS | PASS | release/firstlaunch lookups under a spaced HOME (fixed #21), disk/branch GIVENs, layer budget |
| 2 (main 02a7e1a) | FAIL: The Deadmines entry saw 1 player (not reproduced; entry-gather evidence added #23); Utgarde Keep: Ingvar reached by revived companions beside the reset boss (fixed #23: revive by summon at the probe) | PASS | PASS | layer budget |
| 3 (main a053d69) | PASS: The Deadmines 19.9 min (Gilnid 1 wipe), Razorfen Downs 7.0, Hellfire Ramparts 4.3, Utgarde Keep 6.6 | PASS (2.1 min) | PASS | layer budget only: integration 999 s > 900 s, one lane (4.28 GB free memory at preflight, below the 5.0 GB two-lane threshold) |

Between runs 1 and 2, #22 made the event boss's health read by GUID at first sight, then every 2 s and once after the fight, after a 22.7 s Vazruden kill outran the read.
