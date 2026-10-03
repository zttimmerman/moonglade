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

Knob sets (final = D, see `openspec/changes/m2-living-world/design.md` "Population profile knobs"):
- **A:** MinLevelChance 0.25, ProbTeleToBankers 0.25, default city weights, `BotActiveAlone` 100 / SmartScale 0, LFG/BG off, `MapUpdate.Threads` 4. Old my.cnf, no fresh start. This was the first boot, which created 290 bot characters.
- **B:** A plus MinLevelChance 0.4, MaxLevelChance 0.05, ProbTeleToBankers 0.75, Stormwind/Orgrimmar weight 1, `DontCacheRandomMovementPaths` 1, mysqld `performance_schema=OFF` and 16 MB log buffer. No fresh start, so bots resumed where run A left them.
- **C:** B plus ProbTeleToBankers 1.0, MinLevelChance 0.45, fresh-world start.
- **D (final):** C with MinLevelChance 0.4.

A worldserver with playerbots disabled (`AiPlayerbot.Enabled = 0`, otherwise m0) has a footprint of **1.49 GB** 60 s after start. playerbots' own startup caches roughly double that, to 2.98 GB with 10 bots. Re-rolling bots (the factory's gear, spell and quest setup) adds about 1 GB more, even for 50 bots.

## Finding: the worldserver limit can't be met with a population

- At **every** measured N, including 50, the worldserver exceeds 4.0 GB as measured here: 4.21 GB at N=50, 4.50 GB at N=150, 4.84 GB at N=300. Beyond a fixed cost of about 4.0 GB (playerbots base plus re-rolled bots), each extra bot adds only about 2-3 MB. No population large enough to put 3 bots in each of the 16 zones fits under 4.0 GB. At N=50, 11 of the 16 zones had fewer than 3 bots. At N=150, Stormwind had 2.
- RSS alone, the literal "resident" figure, stays at 2.8 GB or less up to N=300. On this Mac, that number reflects how much macOS has swapped out, not what the server uses.
- **Host free + inactive ≥ 3.0 GB** is dominated by the other apps on this Mac. It dipped below 3.0 at N=150, 250 and 300, and sat at exactly 3.00 at N=50. With the idle m0 stack it read 3.05.
- mysqld (0.69-0.70 GB after the my.cnf change) and world update time (p95 18 ms or less, median 2 ms or less) are well within budget at every N.

**Chosen N = 300.** It is the size whose zone coverage holds with margin under the final knobs (see the N=300 row from the slow run, appended below). Smaller N saves little memory (about 0.6 GB from 300 down to 50) and loses coverage. The budget is **not** met at N=300 (worldserver memory, host memory), and `perf.server_budget` / `perf.population_tuned` fail on it. That is reported to the orchestrator as an unreachable threshold, with these measurements. The spec is unchanged.

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
  {"n": 300, "knobs": "A", "source": "harness/out/pop-session-N300-1790988992.json",
   "summary": {"worldserver_mem_gb": {"min": 4.751, "median": 4.804, "max": 4.839, "n": 61}, "mysqld_mem_gb": {"min": 1.015, "median": 1.017, "max": 1.018, "n": 61}, "update_p95_ms": {"min": 5, "median": 7, "max": 13, "n": 61}, "update_median_ms": {"min": 1, "median": 2, "max": 2, "n": 61}, "host_free_inactive_gb": {"min": 2.033, "median": 3.432, "max": 3.954, "n": 61}}}
 ]
}
```
