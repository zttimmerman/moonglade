# M4 companions: full-run prototype (The Deadmines)

Change `m4-dungeon-companions`, task 1.1, 2026-10-11, branch `spike/m4-full-run`. The question: can the `/mg party` companion party, driven headless by the harness, clear The Deadmines boss by boss?

## Method
- **Party:** the dungeon probe (`Hsdungeon`, a level-19 human warrior, The Deadmines' `LFGDungeons.dbc` target level) sends the add-on's damage-dealer plan (`MG_PARTY_PLAN` via `hslib/partyplan.py`, the same `add_companion` as `dng.party_preset`): a prot warrior tank, a holy priest, a frost mage and a combat rogue. Never PLAYER.
- **Scaling:** AutoBalance on (profile `scaling-on`). The probe enters with GM mode off, so `.ab mapstat` counts 5 players: the dungeon is at stock strength (world health multiplier 1.0, Rhahk'Zor 4490 HP). Then the probe switches GM mode on and only watches. So **four companions fight a 5-player-strength dungeon**: a stricter test than a real run, where the player fights too.
- **Driver:** `dungeon.run_party` (design.md §4), run by the gate check `dng.party_clear` (a prototype version of it at the time; never in a PR tier).
  - Order: the required encounters (a static spawn of the credit entry), by `DungeonEncounter.dbc` orderIndex: Rhahk'Zor, Gilnid, Mr. Smite, Cookie, Captain Greenskin, Edwin VanCleef. Sneed (encounter 162) has no static spawn and is reported as skipped.
  - Staging (`dungeon.nav_stages`): see "Staging" below.
  - Per pull: GM teleport to the staging point; whisper `summon` until all four are within 15 yd and nobody has fought for 5 s (or 60 s; from run 6 on, the gather also ends as soon as the boss is in a fight); select the boss; whisper `tank attack` and `attack` to the tank. If nothing fights the boss within 3 s, the probe steps along the path toward the boss and the order is repeated. Poll the encounter bit (`completedEncounters` in the probe's bind) and each companion's `.pinfo` "Alive" every second.
  - Wipe: all four dead. `.revive` each one, back to staging, wait for the boss to be at full health and out of combat, and pull again. At most 2 wipes (3 pulls). A pull where nothing touched the boss for 60 s is abandoned and counts like a wipe.
- **Evidence:** `harness/out/m4-full-run/` (not committed): `party.log` progress, `party-<time>.json` full evidence per run.

## GM permissions
Read from the world DB `command` table (security per command):

| Command | Security | Dungeon account (2) |
|---|---|---|
| `.go xyz`, `.gps`, `.gm` | 1 | has it |
| `.npc info`, `.revive`, `.damage`, `.die`, `.cast target`, `.instance unbind`, `.summon` | 2 | has it |
| **`.mmap path`** | **3** | **lacks it** |

What I did (design.md §4): a separate harness account at level 3, `HSDNGNAV` / `Hsdngnav` (`paths.DUNGEON_NAV_*`, reserved in `play.py` like the other harness accounts; never PLAYER). It does only the navmesh pass: alone, in GM mode, in a fresh copy of its own, before the party session. Staging points are geometry, so they're the same in any copy. The dungeon account stays at level 2.

## Staging
- **`.mmap path` parses headless.** It prints `Result / Length / Type`, `StartPosition`, `EndPosition` and `ActualEndPosition` (`cs_mmaps.cpp` HandleMmapPathCommand). The path is the *selected unit's* path to the player, so the probe stands on the candidate and selects the boss.
- **The design's straight-line rule mostly fails in The Deadmines.** The candidate 30 yd back toward the previous staging point is inside rock or on another deck for 5 of 6 bosses: NOPATH (type 8) for Rhahk'Zor, snapped 10.6–23 yd off for Gilnid, Mr. Smite, Cookie and VanCleef. For Greenskin the line's point snapped fine but landed on the lower deck: 30 yd away in a straight line, 280 yd on foot (measured along the path in run 4). The tank was ordered to attack from there and never did (no line of sight: `AttackAction::Attack` refuses silently unless verbose). That cost three stalled pulls in runs 1–3.
- **The fallback needs the path's points, and the probe can read them.** `.mmap path` summons a `VISUAL_WAYPOINT` creature (entry 1, 9 s) on every path point. The probe now parses create blocks (`probe.client.parse_update_blocks`, opt-in `track_creates`) and reads them: 31–136 points per boss. Then "last point on the path (walking in) ≥ 25 yd from the boss" works as designed.
- **Added to the rule (prototype):** the line's point is accepted only if it is ≤ 45 yd from the boss on foot along that path. The pull also walks the path toward the boss if the tank doesn't engage.

## Runs
Eight party runs, 2026-10-11, about 8–25 min each, all at 5-player (stock) strength. Runs 1–3 had the Greenskin staging bug. Run 2 was interrupted, so it has no evidence file, only its log. Runs 4–8 are the fixed driver.

| Run | Driver | Result | Bosses killed (of 6) | Wipes (per boss) | Total |
|---|---|---|---|---|---|
| 1 | line rule as designed | not cleared: Greenskin never engaged (3 stalled pulls) | 4 | Gilnid 1 | 1479 s |
| 2 | + path-point approach | interrupted at Greenskin (same stall) | 4 | Rhahk'Zor 1, Gilnid 2 | — |
| 3 | + approach on silence | not cleared: Greenskin never engaged | 4 | Rhahk'Zor 1 | 984 s |
| 4 | + walking-distance check | **cleared**, mask 125 = every required bit | 6 | Gilnid 2 | 830 s |
| 5 | same | **cleared**, mask 125 | 6 | Gilnid 1, Mr. Smite 1 | 794 s |
| 6 | + gather ends when the boss engages | **cleared**, mask 125 | 6 | Rhahk'Zor 1, Gilnid 1, Mr. Smite 1 | 931 s |
| 7 | same | **not cleared: Gilnid, 3 wipes** (the 2nd "wipe" was companions revived next to the boss and its adds, killed again before the pull) | 1 | Gilnid 3 | 458 s |
| 8 | + revive only after the boss resets | **cleared**, mask 125 | 6 | Rhahk'Zor 1, Gilnid 2, Mr. Smite 2 | 1306 s |

### Per encounter, run 6 (the best-instrumented run)
"Time" covers every pull of that boss, from the pull or the boss's engagement to the encounter bit (or the wipe). "+ gather" adds the summon and gather. Deaths are counted by `.pinfo` polling, by plan role. "Killed by party" means the encounter bit was set in the probe's saved `completedEncounters`, which needs players' damage. The probe was in GM mode and dealt none.

| Boss | Pulls | Wipes | Deaths by role | Time | + gather | Killed by party |
|---|---|---|---|---|---|---|
| Rhahk'Zor | 2 | 1 | tank 1, healer 1, damage 2 | 124.9 s | 140.7 s | yes |
| Gilnid | 2 | 1 | tank 1, healer 1, damage 2 | 72.5 s | 106.2 s | yes |
| Mr. Smite | 2 | 1 | tank 1, healer 1, damage 2 | 151.1 s | 206.0 s | yes |
| Cookie | 1 | 0 | — | 44.1 s | 75.1 s | yes |
| Captain Greenskin | 1 | 0 | — | 44.2 s | 61.3 s | yes |
| **Edwin VanCleef** | 1 | 0 | — | **50.5 s** | 57.3 s | **yes** |
| **Total** | 9 | 3 | 12 (all in the 3 wipes) | | **930.6 s** (incl. nav pass, entry, party) | mask 125 |

### Run 8 (the final driver), per encounter
| Boss | Pulls | Wipes | Deaths by role | Time | + gather |
|---|---|---|---|---|---|
| Rhahk'Zor | 2 | 1 | tank 1, healer 1, damage 2 | 33.7 s | 46.1 s |
| Gilnid | 3 | 2 | tank 2, healer 2, damage 5 | 265.1 s | 287.7 s |
| Mr. Smite | 3 | 2 | tank 2, healer 2, damage 5 | 322.3 s | 381.1 s |
| Cookie | 1 | 0 | — | 48.4 s | 61.8 s |
| Captain Greenskin | 1 | 0 | — | 57.9 s | 104.0 s |
| Edwin VanCleef | 1 | 0 | — | 26.3 s | 28.9 s |

### Per encounter, runs 4 and 5
In these runs the gather waited its full 60 s while the companions were already fighting, so "time" there undercounts. Use "+ gather".

| Boss | Run 4: pulls / wipes / deaths / + gather | Run 5: pulls / wipes / deaths / + gather |
|---|---|---|
| Rhahk'Zor | 1 / 0 / — / 66 s | 1 / 0 / — / 64 s |
| Gilnid | 3 / 2 / tank 2, healer 2, damage 5 / 189 s | 2 / 1 / tank 1, healer 2, damage 3 / 132 s |
| Mr. Smite | 1 / 0 / — / 144 s | 2 / 1 / tank 1, healer 1, damage 2 / 69 s |
| Cookie | 1 / 0 / — / 73 s | 1 / 0 / damage 1 / 96 s |
| Captain Greenskin | 1 / 0 / — / 71 s | 1 / 0 / — / 73 s |
| Edwin VanCleef | 1 / 0 / — / 67 s | 1 / 0 / — / 64 s |

### Selfbot solo (optional), the first two bosses
`dng.solo_two_deadmines`: `.playerbots bot self` on the probe, `initself=rare`, alone, so AutoBalance counts 1 player (world health multiplier 0.167). The probe stands 6 yd from the boss.

| Boss | Boss max HP (solo-scaled) | Result | Fight | Damage taken |
|---|---|---|---|---|
| Rhahk'Zor | 751 | killed, 0 deaths | 13.5 s | 52 |
| Gilnid | 486 | killed, 0 deaths | 20.9 s | 178 |

The self-AI works headless. It matches `scale.solo_final_boss`'s VanCleef kills in docs/research/m4-scaling-tuning.md.

## Findings
1. **The party can kill VanCleef. This is not the classic tactics gap (design §7).** VanCleef died on the first pull in all four runs that reached him (runs 4, 5, 6 and 8: 26–51 s, no deaths), and so did Greenskin and Cookie. The task's stop condition, "can't kill VanCleef in 3 attempts", isn't met.
1. **But a full clear isn't reliable yet: 4 of 5 fixed-driver runs cleared.** Run 7 ran out of pulls at Gilnid. Gilnid and Mr. Smite took the full 3 pulls in run 8 (2 wipes each). Every wipe is at one of the first three bosses.
2. **The danger is the first three bosses, and it comes from arrival, not from tactics.** Every wipe (20 across runs 1–8) was at Rhahk'Zor, Gilnid or Mr. Smite. Most started while the companions were still arriving: the summoned bots land at a staging point ~25 yd from a boss with trash around, the tank's `tank` kit engages whatever is close, and the healer may not be there yet. The pull after the wipe, against a reset boss with the trash dead, always won. With 4 fighters at 5-player strength (the probe only watched), that's the margin you'd expect. A real run has the player as the fifth fighter and clears the trash on the way.
3. **The design's staging rule needs two changes** (they are design.md §4 updates for the change, not done here):
   - The straight-line point is only usable when it's also close on foot. In The Deadmines, 5 of 6 candidates are in rock or off the navmesh, and one (Greenskin) was 30 yd away straight but 280 yd on foot (another deck). Rule added: accept the line's point only if it's ≤ 45 yd from the boss along the path.
   - The fallback needs the path's points. `.mmap path` prints only start/end/actual end, but its waypoint summons can be read from the probe's create packets. That parser (`parse_update_blocks`) is new here, with unit tests.
4. **`tank attack` needs line of sight, and fails silently.** `AttackAction::Attack` refuses a target out of sight and only says so when verbose. The pull now walks the path toward the boss until something fights it.
5. **GM permissions:** the dungeon account (level 2) lacks only `.mmap path` (level 3). Handled with the separate level-3 `HSDNGNAV` account for the navmesh pass. Everything else the driver uses is level ≤ 2.
6. **Not measured:** healer mana (not readable headless; the gather doesn't wait on it) and the player as a fifth fighter (the probe watched in GM mode).

## Recommendation
**Proceed** with `m4-dungeon-companions` as planned. No playerbots patch and no tactics work for The Deadmines (contingent task 5.1 not triggered). The boss fights themselves are fine. What needs work is the driver's approach to the first three bosses (4/5 clears, 0–2 wipes each), and that has to be fixed before `dng.party_clear` can be a pass/fail gate. Before task 3.4 builds `dng.party_clear` on this driver:
- **Fold the staging changes into design.md §4:** the walk check, path points from the waypoint creates, and the approach walk.
- **Gather before teleporting into reach.** Summon the party to a point farther back (e.g. the previous staging point), then walk in together, so the wipes-on-arrival stop counting as boss wipes. If wipes on the first three bosses persist, consider a party-size test with the player as a selfbot fifth fighter before any tactics work.
- **Wipe recovery:** revive only after the boss has reset (fixed in run 8). Revived where they fell, the companions were killed again by Gilnid's adds before the next gather (run 7).
- **Budget:** a full Deadmines clear takes ~13–22 min wall time with the driver. That fits `dng.run_budget`'s slow-tier estimate for one dungeon. Four dungeons won't fit in an hour without trimming.

## Interim summary (written at the 1.5 h mark, 22:00; kept as written)
- Runs 1–3: the party killed Rhahk'Zor, Gilnid, Mr. Smite and Cookie every time (0–2 wipes per boss). Captain Greenskin was never engaged, because of the staging bug above (now fixed). So VanCleef hasn't been tried yet.
- Wipes happen mostly during the gather. The bots arrive one at a time at a staging point with trash nearby, and the pack or the boss kills them before the pull. The next pull, against the reset boss with the trash dead, wins.
- Decision: continue. Run 4 has the walking-distance fix, and it needs VanCleef data.

## Slice 3.4: party clear as a check (2026-10-12)

Change `m4-dungeon-companions`, task 3.4, The Deadmines only, branch `change/m4-party-clear`. The prototype's driver lost pulls at the first three bosses while companions were still arriving. Its probe only watched, so four companions fought a dungeon scaled for five players. This slice makes two changes to the driver (design.md §4, "As built (task 3.4)") and turns the run into the gate check `dng.party_clear`. It replaces the prototype's Deadmines-only check.

- **Gather farther back, walk in together.** The companions are told to `stay`. The probe (GM mode on) teleports to a gather point on the boss's walking path, at least 20 yd farther from the boss on foot than the staging point, and summons them there. Once everyone is alive, within 15 yd and out of combat for 5 s (or after 120 s), the probe hops along the path to the staging point and waits for the party at each hop. At the staging point the same readiness wait runs again, and only then is the pull sent. A pull sent before the party is ready fails the check.
- **The probe fights as the fifth member.** It enters with GM mode off, so AutoBalance counts 5 players (world health multiplier 1.0, Rhahk'Zor at 4490 HP). It gets `initself` gear. At the pull it turns GM mode off, orders the tank in, then switches on `.playerbots bot self`. The self-AI goes off again when the probe dies and after each pull. A selfbot that leads the group releases its spirit as soon as it dies (playerbots `AutoReleaseSpiritAction::ShouldAutoRelease`: `groupLeader == bot`), and the ghost lands in a graveyard outside the dungeon.
- **Fidelity note.** While the selfbot is on, the companions' master isn't a "real player" (`IsRealPlayer`: no PlayerbotAI attached), so a companion that dies mid-fight also releases its spirit at once and leaves the map, instead of waiting for a rez. The driver revives everyone after each pull, and the next gather summons them back.

### Runs
Three runs, all with the selfbot fighting and 5 players counted. "Early" means the boss was already fighting (a companion engaged it) before the pull order, so no order was sent. The names are the members who were still more than 15 yd away at that moment.

| Run | Driver | Result | Wipes (per boss) | Total |
|---|---|---|---|---|
| 1 | gather back + walk in + selfbot | **cleared**, mask 125, PASS | none | 871 s |
| 2 | + companions `stay` before the probe's teleport (run 1: they ran after it through trash and died on the way) | cleared, mask 125, but **FAIL**: Mr. Smite's first pull went out unready (2 dead after 120 s of unbroken trash combat) | none (that pull timed out after 300 s) | 1192 s |
| 3 | + GM mode stays on until the pull (run 2: the probe stood at the staging point with GM off and no AI, and the trash fight around it never ended) | **cleared**, mask 125, PASS | Mr. Smite 1 | 1066 s |

Per boss (pulls / wipes / deaths by role / fight time / time including the gather / early engagement):

| Boss (scaled max HP) | Run 1 | Run 2 | Run 3 |
|---|---|---|---|
| Rhahk'Zor (4490) | 1 / 0 / — / 18 s / 21 s / early, all present | 1 / 0 / — / 17 s / 19 s / early, all present | 1 / 0 / — / 29 s / 31 s / no |
| Gilnid (2904) | 1 / 0 / probe 1, tank 1 / 109 s / 220 s / early, tank away | 1 / 0 / — / 46 s / 92 s / no | 1 / 0 / tank 1, healer 1, damage 1 / 56 s / 59 s / early, tank and rogue away |
| Mr. Smite (3872) | 1 / 0 / probe 1 / 73 s / 145 s / early, all present | 2 / 0 (one 300 s timeout) / tank 1, healer 1, damage 1, probe 1 / 376 s / 499 s / pull 1 **unready**; pull 2 early, 3 away | 2 / 1 / tank 1, healer 1, damage 2, probe 1 / 94 s / 162 s / early both times; pull 2 with healer and mage away |
| Cookie (2904) | 1 / 0 / — / 39 s / 39 s / early, healer and mage away | 1 / 0 / damage 1 / 12 s / 14 s / early, mage away | 1 / 0 / — / 13 s / 15 s / early, mage away |
| Captain Greenskin (2904) | 1 / 0 / — / 44 s / 47 s / no | 1 / 0 / — / 40 s / 43 s / no | 1 / 0 / — / 24 s / 27 s / early, all present |
| Edwin VanCleef (3872) | 1 / 0 / — / 13 s / 13 s / early, tank and rogue away | 1 / 0 / — / 22 s / 22 s / early, tank and rogue away | 1 / 0 / — / 33 s / 35 s / early, all present |

### Findings
1. **The wipes moved from 0–2 per boss to 0–1 per run.** The prototype's final driver had 2–5 wipes per run, all at the first three bosses. This slice had 0, 0 and 1 (Mr. Smite in run 3). Every boss died on its first pull except Mr. Smite in runs 2 and 3. All three runs set every required bit (mask 125), in 14.5–20 min.
2. **The bosses usually start fighting before the order.** In 15 of 20 pulls the boss was already fighting before the pull order. In 9 of those, one or more companions were still more than 15 yd away: they hadn't caught up from the gather or the walk-in, or they were fighting trash. This happened most at Cookie and VanCleef, whose gather points are close to the previous boss's room. The staging point is at least 25 yd from the boss, but the companions' own targeting reaches farther. The check fails only when the driver sends its own pull unready. It doesn't fail when a companion engages early; that's recorded per pull.
3. **The gather point itself is often in trash.** It took up to 124 s per boss (the 120 s limit was hit at Gilnid and Mr. Smite in run 3). Companions died there in runs 1 and 2. The driver revives the dead only once combat has stopped for 5 s, as a player would rez.
4. **Not done in this slice:** the other three listed dungeons, `dng.solo_clear` (`dng.solo_two_deadmines` is unchanged), and `dng.run_budget`. Next: reduce early engagement, for example by holding the companions at the gather point until the walk-in, or by a staging point farther from the boss when it has trash nearby. Then check `dng.party_clear` 3× in a row before the other dungeons.
