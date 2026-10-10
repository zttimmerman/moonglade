# M4 dungeon scaling: solo tuning runs

Change `m4-dungeon-scaling`, task 3.4, 2026-10-10. Zach's answer to proposal question 1: a solo fight should feel like a normal group's fight at the dungeon's suggested level, and (question 2) never harder than the unchanged dungeon. This file records every tuning run and why the shipped values are what they are.

## Method
- **Driver:** `harness/hslib/dungeon.py` `solo_final_boss`, the same routine as the slow check `scale.solo_final_boss`.
- **Character:** the dungeon checks' human warrior (`Hsdungeon`), set to each dungeon's `LFGDungeons.dbc` target level and geared by `.playerbots bot initself=rare`. Rare is `AiPlayerbot.AutoGearQualityLimit = 3`; the gear is re-rolled each run, so max health varies between runs.
- **Fight:** fought by the playerbots self-AI, alone, GM mode off, in a fresh copy. The probe is GM-teleported 6 yd from the final boss toward the entrance. Hellfire Ramparts' final boss is summoned by an event, so for that dungeon the probe stands on the first Hellfire Sentry's summon point (`dungeon.EVENT_STAGE`, cited from the core script).
- **Pass:** the boss dies within 10 min and at most 2 attempts.
- **Measured:** fight length (from GM off to the encounter's bit in the saved mask), the boss's scaled max health and `.ab creaturestat` multiplier, the probe's lowest health (from its own update fields), damage taken, and deaths.
- **Knobs:** module tuning settings, not game data (CLAUDE.md §7). `CurveCeiling` stays 1.0, so five players always meet the unchanged dungeon (`scale.party_stock`), and every smaller party meets something weaker.
- **Curve:** AutoBalance's multiplier for n players in a 5-player dungeon is `tanh((n − 5·InflectionPoint)/1.5)`, mapped onto [CurveFloor, CurveCeiling] and normalised so that n = 5 gives exactly the ceiling (`ABUtils.cpp` `getDefaultMultiplier`). Raising `CurveFloor` raises every party size below five together, trash and bosses alike, and leaves five at 1.0.

| CurveFloor (InflectionPoint 0.5, BossModifier 1.0) | multiplier, 1 / 3 / 5 players |
|---|---|
| 0.0 (module default) | 0.123 / 0.684 / 1.000 (measured, `scale.counts_companions`) |
| 0.05 (shipped) | 0.167 (measured) / ~0.70 / 1.000 |
| 0.2 | 0.299 (measured) / ~0.75 / 1.000 |

## Runs
"Lowest" is the probe's lowest health in the fight, as a fraction of its max health at the time.

| Run | CurveFloor | Dungeon (level) | Boss scaled max HP | Result | Fight | Lowest health | Damage taken |
|---|---|---|---|---|---|---|---|
| defaults | 0.0 | Deadmines (19) | 478 (×0.123 of 3872) | killed, 0 deaths | 19.7 s | 324 (max not captured) | 151 |
| defaults | 0.0 | Razorfen Downs (36) | 1079 | killed, 0 deaths | 28.2 s | 1062 | 198 |
| defaults | 0.0 | Hellfire Ramparts (63) | event | not started: the stand point was 30 yd from the sentries, so no fight. Fixed by `EVENT_STAGE` | — | — | — |
| defaults-ramparts | 0.0 | Hellfire Ramparts (63) | event | killed, 0 deaths | 26.5 s | 10559 / 11467 (92 %) | 531 |
| defaults | 0.0 | Utgarde Keep (72) | 14830 | killed, 0 deaths | 36.5 s | 5930 | 3143 |
| floor0.2 | 0.2 | Deadmines | 1157 (×0.299) | killed, 0 deaths | 36.4 s | 743 / 963 (77 %) | 191 |
| floor0.2 | 0.2 | Razorfen Downs | 2612 | killed, 0 deaths | 56.1 s | 2026 / 2661 (76 %) | 552 |
| floor0.2 | 0.2 | Hellfire Ramparts | event | killed, 0 deaths | 40.8 s | 1134 / 4627 (25 %) | 3806 |
| floor0.2 | 0.2 | Utgarde Keep | 35886 | **2 wipes, not killed** | died at 34.0 s and 25.4 s | 0 | 14140, 12100 |
| floor0.05 ×2 | 0.05 | Utgarde Keep | 20094 (×0.167) | killed, 0 deaths (both) | 53.8 s, 53.7 s | 1721 / 10193 (17 %), 1701 / 10003 (17 %) | 9710, 9833 |
| check run 1 | 0.05 | Deadmines | 648 | killed, 0 deaths | 19.7 s | 887 / 1073 (83 %) | 157 |
| | | Razorfen Downs | 1463 | killed, 0 deaths | 30.9 s | 2599 / 2811 (92 %) | 129 |
| | | Hellfire Ramparts | event | killed, 0 deaths | 22.5 s | 4218 / 4777 (88 %) | 700 |
| | | Utgarde Keep | 20094 | killed, 0 deaths | 50.6 s | 2946 / 9843 (30 %) | 7551 |
| check run 2 | 0.05 | Deadmines | 648 | killed, 0 deaths | 25.2 s | 1549 / 1643 (94 %) | 63 |
| | | Razorfen Downs | 1463 | killed, 0 deaths | 25.2 s | 883 / 1161 (76 %) | 226 |
| | | Hellfire Ramparts | event | killed, 0 deaths | 40.8 s | 8800 / 10367 (85 %) | 1300 |
| | | Utgarde Keep | 20094 | killed, 0 deaths | 70.1 s | 10814 / 17538 (62 %) | 5767 |
| check run 3 | 0.05 | Deadmines | 648 | killed, 0 deaths | 19.6 s | 1244 / 1343 (93 %) | 64 |
| | | Razorfen Downs | 1463 | killed, 0 deaths | 28.1 s | 919 / 1211 (76 %) | 209 |
| | | Hellfire Ramparts | event | killed, 0 deaths | 46.9 s | 8809 / 10967 (80 %) | 1867 |
| | | Utgarde Keep | 20094 | killed, 0 deaths | 73.0 s | 11351 / 17668 (64 %) | 4661 |

(Check runs 1–3 are `harness/hs run --tier slow --only scale.solo_final_boss`, all PASS.)

## Decision: `CurveFloor = 0.05` (normal and heroic), everything else at the module defaults
- **The binding constraint is the hardest listed boss.** At floor 0.2, a solo party of one faces 30 % strength. The classic bosses become real fights (36–56 s, and the Ramparts fight takes the warrior to 25 %), but Ingvar the Plunderer kills the level-72 warrior twice in 25–34 s, so `scale.solo_final_boss` fails.
- **At 0.05 (solo multiplier 0.167, 36 % stronger than the module default), every listed boss dies first try.** Ingvar is a close fight: 50–73 s, and the warrior drops to 17–64 % health. The classic bosses stay short (20–47 s) and leave the warrior at 76–94 %. That is closer to "a normal group's fight" than the default and stays within what the check (and a careful player) can win.
- **Heroic uses the same floor for consistency.** No heroic solo fight was measured (M4's solo scenario covers normal difficulty only).
- **What this doesn't settle:** the classic dungeons are still on the easy side for a warrior played by the self-AI. A human player with potions and better play has more margin, and other classes differ. Whether the solo fight "feels right" is the `[human-verify]` scenario at the M4 human gate. If it feels too easy in classic dungeons, the next lever is a per-dungeon setting (`AutoBalance.InflectionPoint.PerInstance` or `.CurveFloor` per instance), not a higher global floor, which the Utgarde Keep runs rule out.
- **Not tuned:** `InflectionPoint` and `BossModifier` stay at the module defaults (0.5 and 1.0). Lowering the inflection point would make three-player parties much harder (closer to stock) without helping the solo balance between classic and Wrath. The floor is the one knob that raised solo difficulty evenly.
