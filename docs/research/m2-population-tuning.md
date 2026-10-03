# M2 population tuning record

m2-living-world task 4.1, builder B12, 2026-10-03. `perf.population_tuned` reads the `json tuning-runs` block at the end of this file. The prose explains the numbers.

**Target Mac:** Apple M2 Pro, 16 GB (`hw.memsize` 17179869184), macOS 26.6.2. The machine was **not idle** during these runs: [redacted] and other apps were open, and swap use stayed between 7.3 and 8.6 GB in every run (`sysctl vm.swapusage`, sampled with each measurement).

**Budget** (performance-budget delta): worldserver resident memory ≤ 4.0 GB; mysqld ≤ 1.0 GB; world update p95 ≤ 100 ms and median ≤ 50 ms; host free + inactive ≥ 3.0 GB. Every 10 s sample of a 10-minute window must stay within all five.

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

## Finding: the budget conflicts with full coverage (reported, not resolved)

- **Worldserver memory vs N** (max of RSS and footprint, worst sample): N=50 → 4.21 GB; N=150 → 4.50; N=250 → 4.61; N=300 → 4.64-4.84. This is above 4.0 GB at **every** measured N. Beyond a fixed cost of about 4.0 GB (playerbots base plus re-rolled bots), each extra bot adds only about 2-3 MB. RSS alone, the literal "resident" figure, stays at 2.8 GB or less up to N=300 (2.41 GB in the final run). On this Mac that figure reflects how much macOS has swapped out (8.1-8.2 GB of swap in use), not what the server uses.
- **Zone coverage vs N** (zones with fewer than 3 bots at about 570 s, fresh start): N=50 → 11 of 16 (3 starting zones plus every capital); N=150 → 1 (Stormwind 2); N=300 → 1 in each of the two slow runs (Thunder Bluff 2, then Undercity 2). Starting zones hold with margin at N=150 and above (5 to 31 bots). Capitals are the weak point, because bots arrive in a burst and leave within minutes.
- **Host free + inactive** is dominated by the other apps on this Mac: minimum 2.03-3.23 GB across runs, and 3.05 GB with only the idle m0 stack.
- **mysqld** (0.69-0.70 GB after the my.cnf change) and **world update time** (p95 18 ms or less, median 2 ms or less) are well within budget at every N.

Per the orchestrator's instruction (2026-10-03), tuning stopped here. Two things block a passing configuration. The 4.0 GB worldserver limit can't be met by any N that covers the 16 zones. Capital coverage at N=300 is still marginal (minimum 2 in both slow runs). `config.POPULATION_N` stays at **300**, the best-covering size measured, so the profile is usable. `perf.server_budget` and `perf.population_tuned` fail on it, and `pop.zones_covered` fails on one capital per run. The spec and thresholds are unchanged. Whether to revise the limits (a spec change with reason) or lower N is the orchestrator's decision.

## Machine-readable runs

`summary` is `population.summarize()` output, copied from the session files `harness/out/pop-session-N*-*.json` (not committed; `hs pop-session` regenerates them). `perf.population_tuned` recomputes which limits each run broke from these numbers.

```json tuning-runs
{
 "chosen_n": 300,
 "runs": [
  {"n": 50, "knobs": "D", "source": "harness/out/pop-session-N50-1790993087.json",
   "summary": {"worldserver_mem_gb": {"min": 4.008, "median": 4.125, "max": 4.21, "n": 61}, "mysqld_mem_gb": {"min": 0.693, "median": 0.693, "max": 0.694, "n": 61}, "update_p95_ms": {"min": 3, "median": 4, "max": 5, "n": 61}, "update_median_ms": {"min": 1, "median": 1, "max": 2, "n": 61}, "host_free_inactive_gb": {"min": 3.001, "median": 3.546, "max": 3.817, "n": 61}}},
  {"n": 150, "knobs": "C", "source": "harness/out/pop-session-N150-1790991778.json",
   "summary": {"worldserver_mem_gb": {"min": 4.414, "median": 4.474, "max": 4.498, "n": 61}, "mysqld_mem_gb": {"min": 0.696, "median": 0.697, "max": 0.697, "n": 61}, "update_p95_ms": {"min": 3, "median": 4, "max": 6, "n": 61}, "update_median_ms": {"min": 1, "median": 1, "max": 2, "n": 61}, "host_free_inactive_gb": {"min": 2.581, "median": 3.269, "max": 3.6, "n": 61}}},
  {"n": 250, "knobs": "B", "source": "harness/out/pop-session-N250-1790990419.json",
   "summary": {"worldserver_mem_gb": {"min": 4.542, "median": 4.591, "max": 4.613, "n": 61}, "mysqld_mem_gb": {"min": 0.695, "median": 0.698, "max": 0.698, "n": 61}, "update_p95_ms": {"min": 5, "median": 7, "max": 18, "n": 61}, "update_median_ms": {"min": 1, "median": 1, "max": 1, "n": 61}, "host_free_inactive_gb": {"min": 2.471, "median": 3.037, "max": 3.786, "n": 61}}},
  {"n": 300, "knobs": "F (final), slow run 2", "source": "harness/out/pop-session-N300-1790999662.json",
   "summary": {"worldserver_mem_gb": {"min": 4.584, "median": 4.621, "max": 4.638, "n": 61}, "mysqld_mem_gb": {"min": 0.703, "median": 0.703, "max": 0.704, "n": 61}, "update_p95_ms": {"min": 5, "median": 5, "max": 7, "n": 61}, "update_median_ms": {"min": 1, "median": 2, "max": 2, "n": 61}, "host_free_inactive_gb": {"min": 3.226, "median": 3.428, "max": 3.639, "n": 61}}},
  {"n": 300, "knobs": "E, slow run 1", "source": "harness/results.json of the 2026-10-03T02:26 slow run (perf.server_budget evidence)",
   "summary": {"worldserver_mem_gb": {"min": 4.529, "median": 4.614, "max": 4.642, "n": 61}, "mysqld_mem_gb": {"min": 0.698, "median": 0.699, "max": 0.699, "n": 61}, "update_p95_ms": {"min": 4, "median": 6, "max": 8, "n": 61}, "update_median_ms": {"min": 1, "median": 1, "max": 2, "n": 61}, "host_free_inactive_gb": {"min": 2.449, "median": 2.852, "max": 3.252, "n": 61}}},
  {"n": 300, "knobs": "A", "source": "harness/out/pop-session-N300-1790988992.json",
   "summary": {"worldserver_mem_gb": {"min": 4.751, "median": 4.804, "max": 4.839, "n": 61}, "mysqld_mem_gb": {"min": 1.015, "median": 1.017, "max": 1.018, "n": 61}, "update_p95_ms": {"min": 5, "median": 7, "max": 13, "n": 61}, "update_median_ms": {"min": 1, "median": 2, "max": 2, "n": 61}, "host_free_inactive_gb": {"min": 2.033, "median": 3.432, "max": 3.954, "n": 61}}}
 ]
}
```
