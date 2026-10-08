# M2 population tuning record

m2-living-world task 4.1, builder B12, 2026-10-03. `perf.population_tuned` reads the `json tuning-runs` block at the end of this file. Rounds: 1 (N=50-300, original budget), 2 (N=500-1500, revised budget, task 2.2). The prose explains the numbers.

**Target Mac:** Apple M2 Pro, 16 GB (`hw.memsize` 17179869184), macOS 26.6.2. The machine was **not idle** during these runs: several large desktop apps were open, and swap use stayed between 7.3 and 8.6 GB in every run (`sysctl vm.swapusage`, sampled with each measurement).

**Budget** (performance-budget delta, revised 2026-10-03 after the first measurements, see the proposal's spec-change log): worldserver ≤ 6.0 GB; mysqld ≤ 1.0 GB; worldserver + mysqld + authserver ≤ 7.0 GB; world update p95 ≤ 100 ms and median ≤ 50 ms. Process memory is max(RSS, footprint). Host free + inactive is recorded, not limited. Every 10 s sample of a 10-minute window must stay within all of these. (The first half of this record was measured against the original guess: worldserver ≤ 4.0 GB and host ≥ 3.0 GB.)

**How it is measured** (`harness/hslib/population.py`, `harness/hs pop-session --n N`, or the slow tier):
- **Process memory** is max(`ps` RSS, `proc_pid_rusage` phys_footprint). Under this Mac's memory pressure, macOS compresses and swaps most of the worldserver heap, so RSS is far below real use. Measured at N=300: worldserver RSS 1.60-2.78 GB against a footprint of 4.75-4.84 GB. `vmmap` on the m0 worldserver shows 3.0 GB malloc-allocated at 0% fragmentation, so the footprint is real data, not slack. Footprint is what Activity Monitor shows as "Memory". Both numbers are kept in every sample.
- **World update time** comes from SOAP `.server info`: the server's own median and p95 over its last 500 world-loop diffs (`cs_server.cpp:283-289`, `UpdateTime.h:26`).
- **Host memory** is `vm_stat` free + inactive pages.

## Runs

The knob sets evolved between runs. Column *knobs* refers to the list below the table.

| N | knobs | worldserver memory GB (min/med/max) | worldserver RSS GB (max) | mysqld GB (max) | update p95 / median ms (max) | host free+inactive GB (min/med) | breaks | zones < 3 bots | moved / progressed |
|---|---|---|---|---|---|---|---|---|---|
| 10 (m0, idle) | m0 | 2.98 → 3.30 over 20 min | 0.89 | 1.01 (old my.cnf) | 9 / 1 | 3.05 | (no window) | n/a | n/a |
| 50 | D | 4.01 / 4.13 / 4.21 | 1.88 | 0.69 | 5 / 2 | 3.00 / 3.55 | worldserver | 11 of 16 | 94% / 68% |
| 150 | C | 4.41 / 4.47 / 4.50 | 2.20 | 0.70 | 6 / 2 | 2.58 / 3.27 | worldserver, host | 1 (Stormwind 2) | 96% / 70% |
| 250 | B | 4.54 / 4.59 / 4.61 | 2.31 | 0.70 | 18 / 1 | 2.47 / 3.04 | worldserver, host | 8 (no fresh start) | 96% / 75% |
| 300 | A | 4.75 / 4.80 / 4.84 | 2.78 | 1.02 (old my.cnf) | 13 / 2 | 2.03 / 3.43 | worldserver, mysqld, host | 3 (no fresh start) | 97% / 74% |
| 300 | E (slow run 1) | 4.53 / 4.61 / 4.64 | n/a | 0.70 | 8 / 2 | 2.45 / 2.85 | worldserver, host | 1 (Thunder Bluff 2) | 97% / 61% |
| 300 | F (final, slow run 2) | 4.58 / 4.62 / 4.64 | 2.41 | 0.70 | 7 / 2 | 3.23 / 3.43 | worldserver | 1 (Undercity 2) | 96% / 62% |

Knob sets (final = F, see `openspec/changes/m2-living-world/design.md` "Population profile knobs"):
- **A:** MinLevelChance 0.25, ProbTeleToBankers 0.25, default city weights, `BotActiveAlone` 100 / SmartScale 0, LFG/BG off, `MapUpdate.Threads` 4. Old my.cnf, no fresh start. This was the first boot, which created 290 bot characters.
- **B:** A plus MinLevelChance 0.4, MaxLevelChance 0.05, ProbTeleToBankers 0.75, Stormwind/Orgrimmar weight 1, `DontCacheRandomMovementPaths` 1, mysqld `performance_schema=OFF` and 16 MB log buffer. No fresh start, so bots resumed where run A left them.
- **C:** B plus ProbTeleToBankers 1.0, MinLevelChance 0.45, fresh-world start.
- **D:** C with MinLevelChance 0.4.
- **E:** D at N=300.
- **F (final, `config.py`):** E plus teleport interval 300-1200 s and capital weights IF/UC/TB 2, SW/Org 1. The capital-retention runs behind this are in design.md, "Population profile knobs".

A worldserver with playerbots disabled (`AiPlayerbot.Enabled = 0`, otherwise m0) has a footprint of **1.49 GB** 60 s after start. playerbots' own startup caches roughly double that, to 2.98 GB with 10 bots. Re-rolling bots (the factory's gear, spell and quest setup) adds about 1 GB more, even for 50 bots.

## Round 2 (task 2.2): default about 500, revised budget

The knobs are set F plus capital weights Stormwind, Orgrimmar and Silvermoon 2, and Thunder Bluff 4 (set **G**; superseded by set H, Round 3 below). With equal weight, Thunder Bluff's intake 60 s after start was 11 bots against 17-25 for the other Horde capitals, and it held 2 bots in slow run 3. Each run is a fresh-world start; samples are the worst of 61.

| N | knobs | worldserver GB | stack total GB | mysqld GB | p95 / median ms | host free+inactive min (recorded) | min starting zone / min capital | moved / progressed |
|---|---|---|---|---|---|---|---|---|
| 500 | G minus three weights | 4.84 | 5.56 | 0.72 | 9 / 2 | 2.51 | 16 / 5 (Stormwind, Orgrimmar) | 96% / 63% |
| 750 | G | 4.94 | 5.67 | 0.73 | 10 / 2 | 2.71 | 27 / 12 (Undercity) | 97% / 62% |
| 1500 | G | 5.34 | 6.09 | 0.75 | 26 / 4 | 2.61 | (all ≥ 3) | 97% / 56% |
| 500 | G, TB weight 2 (slow run 3) | 4.78 | 5.54 | 0.75 | 9 / 2 | 2.61 | 17 / **2** (Thunder Bluff) | 96% / 67% |
| 500 | G final (TB weight 4), session b | 4.80 | 5.56 | 0.75 | 8 / 2 | (in session file) | 22 / 6 (Undercity, Silvermoon) | 95% / 63% |
| 500 | G final, slow run 4 | 4.81 | 5.57 | 0.75 | 8 / 2 | 2.67 | 14 / 5 (Undercity) | 95% / 59% |
| 500 | **H** (UC 4, TB 3), B14 session 1 | 4.79 | 5.56 | 0.76 | 11 / 2 | 3.09 | 15 / 8 (Orgrimmar) | 96% / 66% |
| 500 | **H** (UC 4, TB 3), B14 session 2 | 4.77 | 5.54 | 0.79 | 10 / 2 | 2.27 | 14 / 7 (Stormwind) | 97% / 66% |
| 500 | **H** (UC 4, TB 3), B14 session 3 | 4.83 | 5.60 | 0.79 | 7 / 2 | 3.22 | 18 / 6 (Orgrimmar) | 98% / 68% |

- **Memory grows slowly with N:** about 0.5 MB per bot from 500 to 1500. The fixed playerbots cost dominates. Even N=1500 is inside every revised limit, with 0.67 GB of headroom on worldserver, 0.9 GB on stack total, and p95 at 26 ms.
- **Largest size measured: N=1500, and it fit** every revised limit. Extrapolating linearly, stack total would reach 7.0 GB at about N=3100. That wasn't measured: it's above the 2000 `world_size` cap. The default of 500 is Zach's choice for the world's feel, and it leaves about 1.2 GB of worldserver headroom and 1.4 GB of stack headroom (performance-budget scenario "The default fits and the headroom is measured", revised 2026-10-03).
- **Presets** (`config.WORLD_SIZES`):
  - **small = 150:** measured 4.50 GB worldserver. Starting zones held 5 or more bots, but one capital had 2, so small trades capital coverage for about 0.3 GB.
  - **normal = 500:** the default.
  - **large = 1000:** between two measured passing points (750 and 1500). It stays below 1500 to leave room for the client, whose smoothness is the human check.
- **Disk:** the bot pool is now 1500 characters on 150 RNDBOT accounts (they are kept; smaller sizes use a subset). The MySQL datadir grew from 1154 MB to 1327 MB across all of M2's sessions.

### Round 3 (B14, 2026-10-03): Undercity weight
With set G (Undercity 2, Thunder Bluff 4), Undercity was the weakest capital at the warm-up snapshot (S0, which `pop.zones_covered` reads) in every one of the 12 N=500 session files: 3 to 9 bots. Thunder Bluff held 9 to 27. Set **H** changes two Horde weights, Undercity 2 → 4 and Thunder Bluff 4 → 3, and leaves the other six as they were. Measured with `harness/hs run --tier slow --only pop.zones_covered --only pop.size_stable --only perf.server_budget`, three fresh-start sessions in a row (each run passed all three checks, 500 bots online at S0 and at S1). Counts are online random bots per capital, at S0 and then at S1 (5 minutes later):

| session | UC | SW | IF | Org | TB | Darn | SMC | Exo | min capital S0 / S1 |
|---|---|---|---|---|---|---|---|---|---|
| 1 (`pop-session-N500-1791050426`) | 13 / 5 | 15 / 10 | 18 / 10 | 8 / 9 | 11 / 4 | 12 / 12 | 9 / 7 | 15 / 12 | **8** / 4 |
| 2 (`pop-session-N500-1791051606`) | 11 / 12 | 7 / 8 | 10 / 8 | 10 / 6 | 8 / 10 | 16 / 19 | 15 / 9 | 10 / 9 | **7** / 6 |
| 3 (`pop-session-N500-1791052786`) | 15 / 11 | 12 / 11 | 13 / 8 | 6 / 7 | 17 / 12 | 14 / 15 | 10 / 10 | 9 / 8 | **6** / 7 |

- At S0, the snapshot the check reads, every capital held at least 5 bots in all three sessions (minimums 8, 7 and 6). The 12 G sessions had S0 minimums of 2 to 9; the two at 2 were from the TB-weight-2 era. Undercity went from 3-9 to 11-15 at S0. Orgrimmar and Silvermoon lost a little share (from 2/10 to 2/11 of Horde city teleports) and held 6-10 and 9-15.
- At S1, the snapshot `pop.zones_covered` doesn't read, the minimum was 4 once (Thunder Bluff, session 1). Capital counts swing within minutes (inflow × residence time; see design.md "Population profile knobs"), so the ≥3 check keeps a margin but not a guarantee.
- Starting zones held 14 or more bots in every session. Replaying the saved sessions through `pop.bots_active`, `pop.fresh_start_scope` and `perf.population_tuned` gives PASS for all three (moved 96-98%, progressed 66-68%). The budget held in all three (`perf.server_budget` PASS): worst worldserver 4.83 GB, stack total 5.60 GB, mysqld 0.79 GB, p95 11 ms, median 2 ms.

## Round 1 finding (superseded by the budget revision)
Against the original limits, worldserver memory was above 4.0 GB at every N (4.21 GB at N=50), and host free + inactive depended on other apps. That led to the 2026-10-03 spec revision.

## Machine-readable runs

`largest_measured` states the largest size measured and whether it fit; `perf.population_tuned` checks both against the runs. `summary` is `population.summarize()` output, copied from the session files `harness/out/pop-session-N*-*.json` (not committed; `hs pop-session` regenerates them). `perf.population_tuned` recomputes which limits each run broke from these numbers.

```json tuning-runs
{
 "chosen_n": 500,
 "largest_measured": {
  "n": 1500,
  "fit": true,
  "note": "every revised limit held at N=1500 (worst: worldserver 5.34 GB, stack 6.09 GB, mysqld 0.75 GB, p95 26 ms, median 4 ms)"
 },
 "runs": [
  {
   "n": 500,
   "knobs": "H (UC 4, TB 3; final), B14 session 1",
   "source": "harness/out/pop-session-N500-1791050426.json",
   "summary": {
    "worldserver_mem_gb": {
     "min": 4.695,
     "median": 4.769,
     "max": 4.789,
     "n": 61
    },
    "mysqld_mem_gb": {
     "min": 0.758,
     "median": 0.758,
     "max": 0.758,
     "n": 61
    },
    "stack_total_gb": {
     "min": 5.465,
     "median": 5.538,
     "max": 5.558,
     "n": 61
    },
    "update_p95_ms": {
     "min": 5,
     "median": 6,
     "max": 11,
     "n": 61
    },
    "update_median_ms": {
     "min": 2,
     "median": 2,
     "max": 2,
     "n": 61
    },
    "host_free_inactive_gb": {
     "min": 3.09,
     "median": 3.311,
     "max": 3.475,
     "n": 61
    }
   }
  },
  {
   "n": 500,
   "knobs": "H (UC 4, TB 3; final), B14 session 2",
   "source": "harness/out/pop-session-N500-1791051606.json",
   "summary": {
    "worldserver_mem_gb": {
     "min": 4.627,
     "median": 4.743,
     "max": 4.771,
     "n": 61
    },
    "mysqld_mem_gb": {
     "min": 0.757,
     "median": 0.757,
     "max": 0.786,
     "n": 61
    },
    "stack_total_gb": {
     "min": 5.426,
     "median": 5.513,
     "max": 5.539,
     "n": 61
    },
    "update_p95_ms": {
     "min": 5,
     "median": 6,
     "max": 10,
     "n": 61
    },
    "update_median_ms": {
     "min": 2,
     "median": 2,
     "max": 2,
     "n": 61
    },
    "host_free_inactive_gb": {
     "min": 2.272,
     "median": 3.371,
     "max": 3.636,
     "n": 61
    }
   }
  },
  {
   "n": 500,
   "knobs": "H (UC 4, TB 3; final), B14 session 3",
   "source": "harness/out/pop-session-N500-1791052786.json",
   "summary": {
    "worldserver_mem_gb": {
     "min": 4.733,
     "median": 4.796,
     "max": 4.83,
     "n": 61
    },
    "mysqld_mem_gb": {
     "min": 0.758,
     "median": 0.785,
     "max": 0.785,
     "n": 61
    },
    "stack_total_gb": {
     "min": 5.532,
     "median": 5.584,
     "max": 5.601,
     "n": 61
    },
    "update_p95_ms": {
     "min": 5,
     "median": 6,
     "max": 7,
     "n": 61
    },
    "update_median_ms": {
     "min": 2,
     "median": 2,
     "max": 2,
     "n": 61
    },
    "host_free_inactive_gb": {
     "min": 3.216,
     "median": 3.535,
     "max": 3.741,
     "n": 61
    }
   }
  },
  {
   "n": 500,
   "knobs": "G (final), slow run 4",
   "source": "harness/out/pop-session-N500-1791013968.json",
   "summary": {
    "worldserver_mem_gb": {
     "min": 4.685,
     "median": 4.785,
     "max": 4.814,
     "n": 61
    },
    "mysqld_mem_gb": {
     "min": 0.746,
     "median": 0.746,
     "max": 0.746,
     "n": 61
    },
    "stack_total_gb": {
     "min": 5.442,
     "median": 5.542,
     "max": 5.571,
     "n": 61
    },
    "update_p95_ms": {
     "min": 6,
     "median": 7,
     "max": 8,
     "n": 61
    },
    "update_median_ms": {
     "min": 2,
     "median": 2,
     "max": 2,
     "n": 61
    },
    "host_free_inactive_gb": {
     "min": 2.674,
     "median": 2.938,
     "max": 3.128,
     "n": 61
    }
   }
  },
  {
   "n": 500,
   "knobs": "G (final, TB weight 4)",
   "source": "harness/out/pop-session-N500-1791011619.json",
   "summary": {
    "worldserver_mem_gb": {
     "min": 4.726,
     "median": 4.774,
     "max": 4.795,
     "n": 61
    },
    "mysqld_mem_gb": {
     "min": 0.749,
     "median": 0.749,
     "max": 0.75,
     "n": 61
    },
    "stack_total_gb": {
     "min": 5.487,
     "median": 5.533,
     "max": 5.556,
     "n": 61
    },
    "update_p95_ms": {
     "min": 5,
     "median": 7,
     "max": 8,
     "n": 61
    },
    "update_median_ms": {
     "min": 2,
     "median": 2,
     "max": 2,
     "n": 61
    },
    "host_free_inactive_gb": {
     "min": 2.787,
     "median": 3.088,
     "max": 3.186,
     "n": 61
    }
   }
  },
  {
   "n": 500,
   "knobs": "G with TB weight 2 (slow run 3)",
   "source": "harness/out/pop-session-N500-1791009712.json",
   "summary": {
    "worldserver_mem_gb": {
     "min": 4.726,
     "median": 4.766,
     "max": 4.784,
     "n": 61
    },
    "mysqld_mem_gb": {
     "min": 0.745,
     "median": 0.745,
     "max": 0.745,
     "n": 61
    },
    "stack_total_gb": {
     "min": 5.482,
     "median": 5.522,
     "max": 5.54,
     "n": 61
    },
    "update_p95_ms": {
     "min": 5,
     "median": 6,
     "max": 9,
     "n": 61
    },
    "update_median_ms": {
     "min": 2,
     "median": 2,
     "max": 2,
     "n": 61
    },
    "host_free_inactive_gb": {
     "min": 2.606,
     "median": 2.957,
     "max": 3.237,
     "n": 61
    }
   }
  },
  {
   "n": 1500,
   "knobs": "G",
   "source": "harness/out/pop-session-N1500-1791007769.json",
   "summary": {
    "worldserver_mem_gb": {
     "min": 5.24,
     "median": 5.278,
     "max": 5.335,
     "n": 61
    },
    "mysqld_mem_gb": {
     "min": 0.747,
     "median": 0.748,
     "max": 0.748,
     "n": 61
    },
    "stack_total_gb": {
     "min": 5.998,
     "median": 6.037,
     "max": 6.094,
     "n": 61
    },
    "update_p95_ms": {
     "min": 10,
     "median": 14,
     "max": 26,
     "n": 61
    },
    "update_median_ms": {
     "min": 3,
     "median": 3,
     "max": 4,
     "n": 61
    },
    "host_free_inactive_gb": {
     "min": 2.608,
     "median": 2.813,
     "max": 3.162,
     "n": 61
    }
   }
  },
  {
   "n": 750,
   "knobs": "G",
   "source": "harness/out/pop-session-N750-1791006570.json",
   "summary": {
    "worldserver_mem_gb": {
     "min": 4.866,
     "median": 4.913,
     "max": 4.937,
     "n": 61
    },
    "mysqld_mem_gb": {
     "min": 0.725,
     "median": 0.727,
     "max": 0.727,
     "n": 61
    },
    "stack_total_gb": {
     "min": 5.602,
     "median": 5.65,
     "max": 5.674,
     "n": 61
    },
    "update_p95_ms": {
     "min": 7,
     "median": 9,
     "max": 10,
     "n": 61
    },
    "update_median_ms": {
     "min": 1,
     "median": 2,
     "max": 2,
     "n": 61
    },
    "host_free_inactive_gb": {
     "min": 2.713,
     "median": 3.009,
     "max": 3.306,
     "n": 61
    }
   }
  },
  {
   "n": 500,
   "knobs": "G (before Stormwind/Orgrimmar/Silvermoon weight 2)",
   "source": "harness/out/pop-session-N500-1791005345.json",
   "summary": {
    "worldserver_mem_gb": {
     "min": 4.723,
     "median": 4.792,
     "max": 4.837,
     "n": 61
    },
    "mysqld_mem_gb": {
     "min": 0.716,
     "median": 0.717,
     "max": 0.718,
     "n": 61
    },
    "stack_total_gb": {
     "min": 5.448,
     "median": 5.518,
     "max": 5.564,
     "n": 61
    },
    "update_p95_ms": {
     "min": 6,
     "median": 7,
     "max": 9,
     "n": 61
    },
    "update_median_ms": {
     "min": 1,
     "median": 1,
     "max": 2,
     "n": 61
    },
    "host_free_inactive_gb": {
     "min": 2.514,
     "median": 3.122,
     "max": 3.436,
     "n": 61
    }
   }
  },
  {
   "n": 50,
   "knobs": "D",
   "source": "harness/out/pop-session-N50-1790993087.json",
   "summary": {
    "worldserver_mem_gb": {
     "min": 4.008,
     "median": 4.125,
     "max": 4.21,
     "n": 61
    },
    "mysqld_mem_gb": {
     "min": 0.693,
     "median": 0.693,
     "max": 0.694,
     "n": 61
    },
    "update_p95_ms": {
     "min": 3,
     "median": 4,
     "max": 5,
     "n": 61
    },
    "update_median_ms": {
     "min": 1,
     "median": 1,
     "max": 2,
     "n": 61
    },
    "host_free_inactive_gb": {
     "min": 3.001,
     "median": 3.546,
     "max": 3.817,
     "n": 61
    }
   }
  },
  {
   "n": 150,
   "knobs": "C",
   "source": "harness/out/pop-session-N150-1790991778.json",
   "summary": {
    "worldserver_mem_gb": {
     "min": 4.414,
     "median": 4.474,
     "max": 4.498,
     "n": 61
    },
    "mysqld_mem_gb": {
     "min": 0.696,
     "median": 0.697,
     "max": 0.697,
     "n": 61
    },
    "update_p95_ms": {
     "min": 3,
     "median": 4,
     "max": 6,
     "n": 61
    },
    "update_median_ms": {
     "min": 1,
     "median": 1,
     "max": 2,
     "n": 61
    },
    "host_free_inactive_gb": {
     "min": 2.581,
     "median": 3.269,
     "max": 3.6,
     "n": 61
    }
   }
  },
  {
   "n": 250,
   "knobs": "B",
   "source": "harness/out/pop-session-N250-1790990419.json",
   "summary": {
    "worldserver_mem_gb": {
     "min": 4.542,
     "median": 4.591,
     "max": 4.613,
     "n": 61
    },
    "mysqld_mem_gb": {
     "min": 0.695,
     "median": 0.698,
     "max": 0.698,
     "n": 61
    },
    "update_p95_ms": {
     "min": 5,
     "median": 7,
     "max": 18,
     "n": 61
    },
    "update_median_ms": {
     "min": 1,
     "median": 1,
     "max": 1,
     "n": 61
    },
    "host_free_inactive_gb": {
     "min": 2.471,
     "median": 3.037,
     "max": 3.786,
     "n": 61
    }
   }
  },
  {
   "n": 300,
   "knobs": "F (final), slow run 2",
   "source": "harness/out/pop-session-N300-1790999662.json",
   "summary": {
    "worldserver_mem_gb": {
     "min": 4.584,
     "median": 4.621,
     "max": 4.638,
     "n": 61
    },
    "mysqld_mem_gb": {
     "min": 0.703,
     "median": 0.703,
     "max": 0.704,
     "n": 61
    },
    "update_p95_ms": {
     "min": 5,
     "median": 5,
     "max": 7,
     "n": 61
    },
    "update_median_ms": {
     "min": 1,
     "median": 2,
     "max": 2,
     "n": 61
    },
    "host_free_inactive_gb": {
     "min": 3.226,
     "median": 3.428,
     "max": 3.639,
     "n": 61
    }
   }
  },
  {
   "n": 300,
   "knobs": "E, slow run 1",
   "source": "harness/results.json of the 2026-10-03T02:26 slow run (perf.server_budget evidence)",
   "summary": {
    "worldserver_mem_gb": {
     "min": 4.529,
     "median": 4.614,
     "max": 4.642,
     "n": 61
    },
    "mysqld_mem_gb": {
     "min": 0.698,
     "median": 0.699,
     "max": 0.699,
     "n": 61
    },
    "update_p95_ms": {
     "min": 4,
     "median": 6,
     "max": 8,
     "n": 61
    },
    "update_median_ms": {
     "min": 1,
     "median": 1,
     "max": 2,
     "n": 61
    },
    "host_free_inactive_gb": {
     "min": 2.449,
     "median": 2.852,
     "max": 3.252,
     "n": 61
    }
   }
  },
  {
   "n": 300,
   "knobs": "A",
   "source": "harness/out/pop-session-N300-1790988992.json",
   "summary": {
    "worldserver_mem_gb": {
     "min": 4.751,
     "median": 4.804,
     "max": 4.839,
     "n": 61
    },
    "mysqld_mem_gb": {
     "min": 1.015,
     "median": 1.017,
     "max": 1.018,
     "n": 61
    },
    "update_p95_ms": {
     "min": 5,
     "median": 7,
     "max": 13,
     "n": 61
    },
    "update_median_ms": {
     "min": 1,
     "median": 2,
     "max": 2,
     "n": 61
    },
    "host_free_inactive_gb": {
     "min": 2.033,
     "median": 3.432,
     "max": 3.954,
     "n": 61
    }
   }
  }
 ]
}
```
