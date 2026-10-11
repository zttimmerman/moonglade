# Design: test pyramid

## 1. Baseline, and what it says
Source: the full run of 2026-10-10 13:08–14:30 UTC (`6c24409`), with 236 records and one lane (lane B fell back: 4.64 GB free < 5.0 GB). It took 4935.6 s of wall time, of which the checks took 4912.2 s and building server binaries took 263 s. The proposal has the tables by kind and by area. Facts from the per-check data:

- **68 stack-down `full` checks took 3072.6 s (63 % of the run).**
  - Almost every one opens its own `./play` session, with its own cold server start and stop, and often its own real client.
  - A cold `hs up` is 24.3 s (`lifecycle.up`). A worldserver restart is 14–16 s (`ready_s` in `era.*`, `pause.disabled_noop`, `saves.atomic`).
  - Real-client sessions (`PlayRun`: `login`, `backdrop1/2`, `firststart`, `res`, `saves`, `ctllive`, `seamless`, `seamlesserr`, `pause`, `mode-stock/patched`) cost 70–170 s each.
- **Shared sessions already exist but are counted unevenly.** Several checks show 0.03 s because the first check of a shared session carries its time (F33). So area totals are reliable and single-check times are not.
- **Real-time waits repeat per scenario:**
  - five pause checks each pause 20 s with their own setup (202 s);
  - `pause.respawn_frozen` waits out a natural respawn (145 s);
  - `pause.disconnect_unpauses` waits 90 s for the character to go offline;
  - `login.cancel_shows_list` watches 60 s;
  - `saves_gui` waits 40 s before confirming;
  - login screenshots wait 17–20 s.
- **Duplicate work:**
  - `report.tiers` re-runs the whole quick tier nested (155.9 s);
  - `client.launch_smoke` starts Wine and the real client just to see it run (38.3 s), which every client session already does.
- **The quick tier is not a unit layer.** It took 180.5 s (177 s against its 180 s budget on recent runs) and needs a server start: 24 of its 77 checks need the stack.

## 2. Layers, tiers and what a check declares
`registry.check(...)` gains these fields:
- `layer`: `"unit" | "integration" | "journey" | "gate"`. It replaces `tier`, and `report.layers_declared` fails a missing one.
- `covers`: a tuple of globs for the non-Python paths whose behaviour the check asserts, such as `modules/mod-moonglade/src/MoongladePause.cpp`, `client-mods/addons/Moonglade/*.lua`, `harness/app/launcher.m` and `patches/client/0003-*`. Python inputs come from the import closure (§8). A glob that matches no file makes the selection FULL (stale map).
- `scope`: `"character"` (touches only the characters the fixture gave it and reads nothing global) or `"exclusive"` (the default). Only `character` tests may share the 2-worker pool.
- `profile`: as today.
- `gui`: kept for the preflight.
- `budget_s`: required for journeys.

The `backups` and `ci_only` markers keep their current meaning: `backups` goes away with the backups tier, and `ci_only` stays for `signing.notarized`.

Run tiers:
- `unit` (also `hs test`). Unit-layer checks and unit tests. Never starts the stack. Builders run this after every task. It has its own fail-fast mode for the inner loop (`hs test -k <pattern>`).
- `pr`. The unit layer first; if it fails, the run stops there, because there is no point paying for acceptance. Then selection (§8), then the selected tests in this order:
  1. the cold integration segment (stack down);
  2. `hs up`;
  3. the shared segment, default profile, then one group per other profile;
  4. warm journeys;
  5. `hs down` (`lifecycle.down`);
  6. `journey.first_play` (cold);
  7. selected gate checks;
  8. post-run reports.
- `full`. The same order with everything selected, plus all gate checks. It runs at milestone gates, three times.

`results.json` gains:
- `meta.layers`: wall time and budget per layer;
- `meta.selection`: changed paths, the reason per selected test, FULL and why, and the selection's duration;
- `meta.unit`: per-test ids, status and duration for the Python, C++ and Lua suites;
- `layer` on every record.

## 3. The unit runner (Python)
- **Runner.** The standard library's `unittest`. pytest isn't installed (`python3 -c "import pytest"` fails on the Homebrew Python 3.14, and `env/tools/venv` holds only git-filter-repo). Installing it is out (no Homebrew installs, and nothing new for friends to set up), and `unittest` is enough.
- **Layout.** `harness/tests/unit/test_<area>.py`. Each test is a `unittest.TestCase` method, and its id is `unit:py:<module>.<Class>.<method>`. That id is what a scenario's `Check:` line names.
- **Execution.** `hs test` loads `unittest.TestLoader().discover("harness/tests/unit")` in-process with a result class that times each test. A test over 2 s fails ("unit test over budget") unless it's on the reviewed fixture allowlist, which only holds the C++ build (§4) and the Lua build (§5).
- **Isolation.**
  - A base class `UnitCase` points `paths` at a fresh temp root (`HS_UNIT_ROOT`) so nothing under `env/` or `client/` is written.
  - At start and end it fingerprints `env/player.json`, `env/settings.json`, `env/saves/` and `env/backups/` (sizes and mtimes, cheap) and fails the test that changed one.
  - It forbids `subprocess` calls to `hs up`, `./play`, `wine` and `mysqld` with a guard, and samples the process table at the layer's end.
- **The existing static checks** (62 records going to the unit layer, appendix A) stay `@check`s with `layer="unit"`.
  - They run in a 4-process pool when they declare `parallel_safe=True`, and serially otherwise.
  - Their slowest forms today are `report.keep_awake` 9.3 s, `power.run_pause` 7.5 s, `app.bundle_valid` 6.7 s, `client.play_revert` 6.2 s and `app.icon` 6.0 s.
  - Each migration slice either keeps them static or rewrites their logic as true unit tests.
- **What becomes true Python unit tests** (pure logic in `harness/hslib`):
  - backups rotation, manifest and verify, and the refusal reasons;
  - `charsaves.render`, list and lookup;
  - the `savesctl` packet parser and listener on a fake socket;
  - `clientcfg` merges;
  - the resolution decision with injected displays;
  - the `frontmenu` file render;
  - the `stages` parser;
  - `lanes.preflight_decision`;
  - the power guard;
  - the runner's tier, selection and budget rules (the synthetic halves of today's `report.*_rule` checks);
  - the `play` lock reclaim and signal mapping (a fake child process);
  - download pin verification;
  - `publicscan`;
  - `srp6` and `probe.proto` encode and decode against recorded vectors;
  - DBC parsing on a scratch DBC.

## 4. C++ logic under test without a server
- **Where.** `modules/mod-moonglade/src/logic/`, with files `SavesLogic.{h,cpp}`, `PauseLogic.{h,cpp}` and, after m4-dungeon-scaling merges, `DungeonsLogic.{h,cpp}`.
  - Rule: these files include only the C++ standard library and `<zlib.h>`, and take everything else as plain parameters (ids, times, flags, small structs). `report.unit_layer`'s scan enforces "no AzerothCore header".
  - The module `.cpp` files call these functions. Nothing is copied, so the module and the tests run the same code.
- **What moves** (from a read of the sources at `699d778`):
  - **MoongladeSaves.cpp** (1988 lines):
    - `JsonEscape`;
    - the manifest and snapshot JSON writer and the small JSON parser (`JVal`);
    - `IsHexType` and `IsFloatType`;
    - `SqlValue`;
    - `CleanLabel` and `DefaultLabel` (the zone name passed in);
    - `FormatTime` (with the time passed in);
    - the retention choice inside `Rotate`: given (id, kind, created) entries, which ones to delete for 5 autos, 1 quick and 3 before-load, kept separate from the filesystem walk;
    - `ListGen` and the reply-line format (at most 255 bytes, newest first);
    - the validation half of `ReadSave` and `CheckCharacter` (damaged hash, other character, other account, schema fingerprint → reason);
    - `RefuseSave`'s predicate over `{inCombat, dead, ...}`;
    - the autosave-interval decision ("inside the interval → none").
  - **MoongladePause.cpp** (343 lines):
    - a `PauseState` machine: request (enabled, onTaxi, onTransport) → reply `paused|refused|disabled`; resume; owner logout or disconnect → resume; the tick acknowledgement;
    - the cooldown remaining-time arithmetic in `ResyncCooldowns` (end, now, paused ms).
  - **MoongladeDungeons.cpp** (branch): the final-boss check `FinalDone(mask, map, difficulty)` with the encounter table passed in, and the keep-or-reset decision in `OnPlayerCanEnterMap`.
- **Test binary.**
  - Tests are in `harness/tests/unit/cpp/*.cpp`, written with a ~100-line single header `harness/tests/unit/cpp/mgtest.h` (our own: `MG_TEST(suite, name)`, `MG_EQ`, TAP-like output `ok|not ok <suite.name> <ms>`). No framework is downloaded.
  - The build is `/usr/bin/clang++ -std=c++20 -O0 -g -Wall -Wextra -Werror -I modules/mod-moonglade/src/logic <logic/*.cpp> <tests/*.cpp> -lz -o <out>/mg_unit`. Apple clang 16.0.0 is on this Mac, and the server build already needs the Command Line Tools.
  - Output goes under `harness/out/unit/` (gitignored). It is rebuilt only when the SHA-256 of its inputs changes; the stamp lives beside the binary.
  - Budget: the build ≤ 60 s (it's ~3k lines, so expected ≈ 10–25 s cold, to be measured in task 1.1), the run ≤ 2 s.
- **The server build picks up `logic/`.** This is unverified: AzerothCore collects module sources by directory, and task 1.1 confirms it with an incremental build before anything else is extracted. If it doesn't, the module gets a `CMakeLists.txt` fragment, which is a module change, not a core patch.

## 5. The Lua runner
- **What's available** (checked 2026-10-10):
  - `/opt/homebrew/bin/lua` is **Lua 5.4.7** from Homebrew;
  - there is no Lua 5.1 or LuaJIT;
  - there is nothing Lua under `env/` (only `env/tools/{venv,actionlint-venv,lssampler}`), `server/` or `data/`.

  The client runs **Lua 5.1**. 5.4 differs in exactly the places glue code uses: global `unpack`, `getn`, `setfenv`, `string.gfind`, integer division and the `#` operator on holes. Homebrew is also not a dependency we add. So we don't use it.
- **The pin.**
  - `fork.lock` gets `lua51_url=https://www.lua.org/ftp/lua-5.1.5.tar.gz` and `lua51_sha256=<computed at download in task 1.1>`. The hash is cross-checked against the checksum lua.org publishes on its download page; the source gets cited in `fork.lock`'s comment, as for the other pins.
  - `downloads.PINS` gains `lua51`.
  - `hs unit-tools` (also run by `hs test` when the stamp is missing) verifies the archive, unpacks it into `env/tools/lua-5.1.5/`, runs `make macosx` with the system `cc` (about 5 s), and stamps the result with the archive hash.
  - The archive is about 220 KB, MIT-licensed, and needs no network at test time once built.
  - A missing or mismatched build fails the Lua suite and is never skipped.
- **The stub runtime** (`harness/tests/unit/lua/wowstub.lua`):
  - `CreateFrame`, with frames that have `SetScript`/`GetScript`, `Show`/`Hide`/`IsShown`, `RegisterEvent` plus an event pump, `SetWidth`/`GetWidth`/`GetTop`/`GetBottom`, and textures and font strings as records;
  - `GetCVar`/`SetCVar`;
  - `DefaultServerLogin`, `GlueDialog_Show`, `EnterWorld`, `SendAddonMessage` and `Screenshot` as recorders;
  - the client's string helpers that the code uses (`strsplit`, `format`, `tinsert`, `wipe`);
  - a clock driven by the test: `GetTime()` plus an `advance(dt)` that calls each visible frame's `OnUpdate(self, dt)`.

  Every stub function names, in a comment, the FrameXML or GlueXML file and the function whose behaviour it imitates, from the installed client's own base archives (§7 provenance: no API behaviour from memory). Only the functions our Lua calls are stubbed. Calling anything else raises "unstubbed API: <name>", so the stub can't silently pretend.
- **XML frames.** A Python step (`ElementTree`) turns `HSFrontMenu.xml` and `Bindings.xml` into a Lua prelude that creates the named frames with their parents and scripts. The real `.lua` files are then loaded unchanged.
- **Simulated time** replaces the live waits: "no login for 60 s after the menu shows" is `advance(0.1)` × 600 with an assertion that `DefaultServerLogin` was never recorded, which takes milliseconds.
- **Test ids** are `unit:lua:<file>.<name>`. The driver is `lua harness/tests/unit/lua/run.lua <test files>`, which prints the same TAP-like lines as the C++ binary.

## 6. One shared server for integration tests
**Segments of a `pr` or `full` run:**
1. **Cold segment** (stack down): the tests that verify `./play`'s own start and stop (play S1–S3, the first-run account, the backup-on-quit ordering and the injected backup failure), the startup-failure tests, and the no-stack integration tests.
2. **`hs up`**, recorded as the fixture's start (today's `lifecycle.up` becomes this record).
3. **Shared segment, default profile:** every other integration test. Character-scoped tests use a 2-worker pool; exclusive tests run alone, with no pool worker running.
4. **Profile groups:** one worldserver restart per profile (pause off; the saves test profile with `CharacterDatabase.WorkerThreads 2`; era off and on, shared by `era.toggle_roundtrip` and `era.scope_guard`), then back to default. `lifecycle.persist` uses one of these restarts as its `hs down`/`hs up` when the timeline allows it (measured in 1.2). Task 1.2 also checks whether `Moonglade.Pause.Enable` honours `.reload config` (`OnAfterConfigLoad(reload=true)` in MoongladePause.cpp suggests it does, but that is to be measured). If it does, the pause-off group needs no restart.
5. **Warm journeys:** `./play` with `HS_PLAY_KEEP_STACK` (harness-control "Warm journeys reuse a running stack only in the harness").
6. **`hs down`** (`lifecycle.down`), then **`journey.first_play`** cold.

**Per-test characters.**
- The fixture creates a uniquely named account and character per test: `HSI<base36 counter>`, at most 12 characters (the client's name limit), never `HSTEST` unless a scenario names it.
- They are created through the existing fixture path (`fixtures.py`, SOAP `.account create` plus the probe's character creation), about 1 s each and measured in 1.2. A test can ask for N characters (saves scope needs A and B).
- At the end of the run, one batch removes them all (SOAP `.character erase` while online; the account rows through the existing account fixture).
- `play.player_account_untouched` guards PLAYER.

**Reset between tests** (budget: median ≤ 1 s, max ≤ 3 s):
- One SOAP round asks for `.moonglade pause status`, the profile marker (`stack.profile_matches`), `.moonglade save interval` (read back) and the online characters, then compares them with the fixture's expected state.
- A test that changes shared state registers an undo, as a pytest-style finalizer on the fixture.
- If the state still differs after the undos, the last test fails with the difference ("leaked state"), the worldserver restarts (14–16 s), and the next test runs clean.
- The reset is fail-closed: a SOAP error counts as dirty.

**Why no database restore between tests:** `acore_world` is large, and a per-test restore would cost more than the tests. Per-test characters isolate the characters database, and the world database isn't written by tests except for scratch spawns, which their tests remove and the reset verifies.

**The 2-worker pool.**
- Only `scope="character"` tests, two at a time, on the same server.
- The pool is drained before any exclusive test.
- Task 1.2 runs the saves slice three times serially and three times pooled, with the order reversed once. If any status differs, the pool stays off, and the fallback levers (§9.3) become required work.

## 7. Real-time waits
| Wait today | Where | Treatment |
|---|---|---|
| 20 s pause × 5 scenarios + 5 setups | `pause.combat/aura/cooldown/cast/bots_frozen` | One setup establishes all five GIVENs (melee, buff + DoT ticking, player and creature cooldowns, a cast in progress, a moving bot), then one 20 s pause, then all five THENs (`pause.freeze_matrix`). The 20 s stays, because the spec says 20 s |
| Natural respawn (145 s) | `pause.respawn_frozen` | A scratch spawn with a short respawn. The GM command for setting a spawn's respawn time in the pinned core is checked in task 1.2 before it's used (not taken from memory). The fallback is a scratch `creature` row inserted with a short `spawntimesecs` and loaded with the existing fixture path |
| 90 s offline grace | `pause.disconnect_unpauses` | A per-test character: the next test doesn't use it, so nothing waits |
| 2 restarts per profile flip | `pause.disabled_noop`, `era.toggle_roundtrip`, `era.scope_guard` | Grouped by profile (§6), and `.reload config` if it applies |
| 60 s "no login" watch | `login.cancel_shows_list` | Lua unit test with a simulated 60 s, plus a 10 s live watch in `journey.login_recovery` (spec-change log) |
| 17–20 s screenshot or Continue timing | `login_checks.PRE_CONTINUE_SHOT_S`, `CONTINUE_AT_S` | Event-driven: the screenshot when the menu reports shown, then Continue |
| 40 s confirm | `saves_gui_checks.LOAD_AT_S` | Poll for the saved level in the list (10 s spec limit) |
| 30 s stay | `client.login_text_clean` | Kept (spec). Optional lever: start it first as a passive observer and assert at the end of the segment (−30 s) |
| 20 s logout countdown | `pause.logout_unpauses` | Kept (spec) |
| 5 min soak | `pause.session_alive` | Gate layer |
| `time.sleep` > 1 s elsewhere | lint `report.no_fixed_sleeps` | Allowlisted only with a spec citation |

## 8. The impact map
**Inputs:**
- the changed paths (existing `runner.changed_paths`, unchanged: merge-base with `origin/main`, plus staged, unstaged and untracked files; `None` if git fails);
- each check's own source file;
- its `covers` globs;
- `runner.import_closure(roots=(check_file,))`.

`import_closure` already handles imports inside functions and relative imports, and it caches parses per file version. One closure per check file (about 40 files), with the shared cache, costs well under a second; it's measured in 1.3.

**Rules** (spec "Impact selection is fail-safe"):
1. A git failure → FULL.
2. Each path:
   - FULL-trigger → FULL;
   - no-impact → nothing;
   - otherwise owners = checks whose `covers` match, plus checks whose own file's closure contains the path;
   - journeys whose `covers` or closure contain the path are added; if a journey only runs through the path (closure) without covering it, `journey.first_play` is added instead of that journey;
   - no owner and no journey → FULL ("unmapped path").
3. `config.py` and `paths.py`: an AST comparison of the old and new file. If only top-level assignments differ, select the checks whose closure files mention any changed name (a word match on the source). Anything else → FULL.
4. A stale `covers` glob → FULL ("stale impact map").
5. Gate checks are selected by the same rules, never by FULL alone. A milestone `full` run is what runs every gate check.

**Why journeys use "covers, else first_play":** every journey runs `./play`, so their closures contain most of the play loop. If running through a path were enough to select a journey, any play-loop change would select all six (9.6 min). `journey.first_play` is the cold, complete flow, so it smoke-tests anything the journeys merely pass through. A journey is selected in full only when it asserts on the changed path. Cross-feature regressions that slip past this are what the milestone-gate full ×3 is for. This is the deliberate trade-off (proposal, Risks).

**Replay tool.**
- `hs select --replay N` takes the last N merged PRs (merge commits on `origin/main`, `git diff --name-only M^1 M`), runs the selector on each, and prices each selection with the per-check times of the last full run (`harness/out/last-full-run/`), using the projected times for migrated tests once they exist.
- It reports each PR's selection, its FULL reason and the projected minutes, plus the mean.
- `report.pr_average` uses it until 5 real pr runs are recorded.

**Rough replay today** (a classification of #19–#33 by their touched paths with the rules above; the real selector in 1.3 replaces it):
| PR | FULL? (rule) | Projected min |
|---|---|---:|
| #19 save-backups | yes (`stack.py`) | 30.4 |
| #20 seamless-experience | yes (`probe/`) | 30.4 |
| #21 easy-setup | yes (`build.py`, `stack.py`, `fork.lock`, `bootstrap`) | 30.4 |
| #22 seamless-polish | no (glue only: J1 + J4 + Lua unit) | 4.7 |
| #23 test-loop-speedups | effectively yes (`util.py` is in every closure) | 30.4 |
| #24 power-guard | yes (`build.py`) | 30.4 |
| #25 second-stack | yes (`stack.py`, `paths.py` functions) | 30.4 |
| #26 go-public | no (`launch.py`, publicscan: unit + J1 + J5 + smoke) | 4.0 |
| #27 release-build | yes (`fork.lock`) | 30.4 |
| #28 release-first-launch | yes (`stack.py`) | 30.4 |
| #29 release-signing | no (app, icon, release tools; selected gate checks are fixed cost) | 6.8 |
| #30 icon-corner-c | no (icon: unit + J6 + J1) | 5.8 |
| #31 saves-channel | no (play loop, savesctl, glue) | 16.3 |
| #32 true-pause | yes (`patches/core`) | 30.4 |
| #33 front-menu-saves | no under rule 3 (constants in `config.py`/`paths.py`), but broad | ≈ 22 |
| **Mean** | 9 of 15 FULL | **22.2** |

The 6 non-FULL PRs average 9.9 min. With FULL at 30.4 min, the ≤ 15 min mean needs at most (15 − 9.9) / (30.4 − 9.9) = **25 %** FULL-trigger PRs. #19–#33 were mostly platform work (setup, release, lanes, power, test loop), at 60 %. §9.5 lists what would close the gap.

## 9. Timing, budgets and the projection

### 9.1 Budgets enforced (spec "Every layer is timed and budgeted")
| Layer | Budget | Measured where |
|---|---|---|
| Unit layer | ≤ 120 s (aim ≤ 60 s); each unit test ≤ 2 s; C++ build ≤ 60 s (timed separately, counted in the 120) | `meta.layers.unit`, `meta.unit` |
| Selection | ≤ 10 s | `meta.selection.duration_s` |
| Each journey | its budget (§10) | journey record |
| Complete integration + journeys (full, or FULL selection) | ≤ 30 min, excluding build and gate | `meta.layers` |
| New selected checks (not in the timing baseline) | ≤ 120 s in a pr run | `timing.new_checks_s` restricted to selected acceptance checks |
| PR mean | ≤ 15 min over the last 10 pr runs | `harness/out/pr-runs.jsonl` |
| Per-check regression | ≤ 110 % of the baseline (unchanged) | `report.timing` |
| Reset between shared-server tests | median ≤ 1 s, max ≤ 3 s | `meta.fixture.resets` |

### 9.2 Projected unit layer
| Part | Arithmetic | Seconds |
|---|---|---:|
| Static unit-layer checks kept as checks | 318.2 s today across the 62 records, minus 220 s for the ones rewritten as true unit tests (`lane.merge` 65.4, `client.resolution_default` 75.7, `backups.rotation` 43.2, `saves.inworld_list` 12.3, `lane.preflight` 11.7, `saves.quick_slot` 4.0, `savesctl.*` 3.8, `saves.list_wording` 1.8, report rules ≈ 2) = 98 s serial; in a 4-process pool ≈ 98 / 4 ≈ 25 s, bounded below by the longest (9.3 s) → ≈ 28 | 28 |
| Checks rewritten as true unit tests | the 220 s above, as in-process tests | 5 |
| New Python unit tests | ≈ 400 tests × ≤ 20 ms | 8 |
| Lua suite | ≈ 100 tests × ≈ 10 ms + interpreter start | 1 |
| C++ run | ≈ 150 tests, < 1 s | 1 |
| Discovery and startup | | 3 |
| Selection-free unit tier total, warm | 28 + 5 (rewritten) + 8 + 1 + 1 + 3 | **≈ 46** |
| When the C++ binary rebuilds | + ≈ 30 (measured in 1.1) | **≈ 76** |

If the pool proves unsafe for some static checks, the serial static part is 98 s, giving about 116 s warm, which is at the budget's edge. In that case each slice must rewrite its slow static checks; the five slowest are 35.7 s in total.

### 9.3 Projected integration layer
**Cold segment** (stack down, own `./play` sessions):
| Test | Today (s) | Projected (s) | Arithmetic |
|---|---:|---:|---|
| play S1 (`loop_clean_exit` + `offline` + `single_instance` + `dock_identity` + `stage_lines` + `world_progress`) | 167.2 | 55 | one sandboxed session ≈ `play.offline`'s 54.7 |
| play S2 interrupt (SIGINT live) | 61.9 | 35 | one session instead of two; SIGTERM and repeated signals in unit |
| play S3 client crash | 32.9 | 33 | unchanged |
| `play.startup_failure`, `lifecycle.failfast`, `app.startup_failure` | 25.5 | 25.5 | unchanged |
| `play.player_account` | 93.8 | 47 | one first-run session of two (93.8 / 2) |
| `backups.on_quit` | 71.1 | 71 | unchanged |
| `backups.failure_nonblocking` | 77.7 | 39 | one injected-failure session of two (77.7 / 2) |
| no-stack: `firstlaunch.client_refusals`, `release.pins`, `harness.prefix_scope` | 13.2 | 13.2 | unchanged |
| **Cold total** | 543.3 | **318.7** | |

**Shared segment** (one `hs up`):
| Test(s) | Today (s) | Projected (s) | Arithmetic |
|---|---:|---:|---|
| fixture start (`lifecycle.up`) | 24.3 | 24.3 | once |
| server smoke: `gm.*`, `sql.*`, `fixture.*`, `client.realmlist`, `client.protocol_*`, `build.playerbots_active`, `lifecycle.idempotent`, `play.player_account_untouched`, `lifecycle.isolated` | 30.6 | 15 | `lifecycle.isolated` 18.6 → ≈ 3 (lsof on our pids only) |
| `lifecycle.persist` | 25.0 | 25 | unchanged |
| `core.login_logout_race` | 5.7 | 5.7 | |
| `login.continue_context`, `client.login_text_clean` | 32.8 | 32.8 | 30 s stay kept |
| era quick ×6 | 22.1 | 22 | |
| `era.toggle_roundtrip` + `era.scope_guard` | 92.2 | 64 | 4 restarts (≈ 30 + 28 s ready) → 2 (≈ 30 s) + their own work (18 + 16 s) |
| `comp.*` | 111.5 | 111.5 | unchanged |
| pause: freeze matrix (5 checks) | 202.2 | 55 | one 20 s pause + ≈ 25 s setup + ≈ 10 s after-checks |
| `pause.respawn_frozen` | 144.6 | 50 | R ≈ 10 s scratch respawn: 2 × (≈ 12 s pause + ≤ 6 s respawn sweep) + ≈ 15 s setup |
| `pause.refused_on_taxi`, `pause.logout_unpauses` | 33.8 | 33.8 | unchanged (20 s countdown is the spec) |
| `pause.disabled_noop` | 60.8 | 45 | one restart pair in the profile group; less with `.reload config` |
| `pause.disconnect_unpauses` | 84.3 | 15 | no 90 s offline wait |
| saves (14 tests that stay integration) | 366.2 | 320.9 | `scope` 66.8 → 50 (2 snapshots, not 3), `refused_in_combat` 24 → 12 (combat only), `load_refusals` 24.5 → 8 (one live case); the rest unchanged |
| `savesctl.test_actions_refused`, `savesctl.hygiene` | 43.5 | 31.8 | hygiene warm: 36.7 → 25 |
| backups: `kill_safe`, `restore_roundtrip`, `restore_refusals` | 371.2 | 130 | 45 + 70 + 15: driven directly on the shared database, no `./play` restarts |
| `app.loading_stages_warm` | 36.8 | 25 | keep-stack: no stop |
| `client.config_gate` (+ the not-rebuilt probe), `client.real_client_refused` | 71.9 | 33 | warm stand-in run 30 + 3 |
| `lifecycle.down` | 5.9 | 5.9 | |
| run guards (saves/backups/config/pristine/not-rebuilt) | ≈ 4 | 4 | |
| **Shared total, serial** | | **1049.7** | |

**Integration, complete:**
- Serial: 318.7 + 1049.7 = **1368.4 s (22.8 min)**.
- With the 2-worker pool: the character-scoped tests are saves (`manual` 7.1, `refused_in_combat` 12, `autosave_triggers` 50.7, `bots_skip` 21.9, `roundtrip` 27.2, `item_ids_fresh` 10.8, `inworld_load` 17.7, `undo` 3.5, `load_atomic` 3.3, `load_refusals` 8: 162.2 s), plus `comp.*` 111.5, era quick 22, login 32.8 and `savesctl.test_actions_refused` 6.8, for 335.3 s. In two workers they take about 335.3 / 2 = 167.7 s, which saves 167.7 s. The total is 1368.4 − 167.7 = **1200.7 s (20.0 min)**.

**Fallback levers** if the pool is off (each measured in 1.2):
| Lever | Saves |
|---|---:|
| `client.login_text_clean` as a passive observer | 30 s |
| one shared scratch play client for the warm journeys | 20 s |
| `lifecycle.persist` folded into the era restarts | 25 s |
| lane B running the shared segment while lane A runs journeys (memory permitting; it fell back on the baseline run) | up to ≈ 9 min |

### 9.4 Projected journeys and the complete suite
| Journey | Replaces (today, s) | Steps | Projected (s) | Budget (s) |
|---|---:|---|---:|---:|
| `journey.first_play` (cold) | 314.0 | stack start 24 + fresh play-client build ≈ 10 + client start to menu ≈ 15 + fresh-menu watch 5 + Continue to character select ≈ 10 + Play to world ≈ 10 + add-on report and logout ≈ 10 + Dock quit, shutdown window and backup ≈ 20 + stack down ≈ 10 | 115 | 150 |
| `journey.pause` | 81.8 | client start ≈ 15 + Continue to world ≈ 20 + Esc, status, banner shot, Esc, resume ≈ 15 + quit 5 | 55 | 90 |
| `journey.saves` | 296.0 | start 15 + six `ctl:S` codes and listener-down ≈ 15 + Continue 10 + Saves panel and older load ≈ 15 + Play 10 + F6 and manual save, exit to character select, list ≈ 20 + quit 5 + slack 20 | 110 | 150 |
| `journey.login_recovery` | 271.9 | start 15 + 10 s no-login watch + Continue with auth down to the friendly error ≈ 10 + details, auth back, Try again to character select ≈ 15 + Main Menu then Continue reload ≈ 15 + quit 5 + slack 15 | 85 | 120 |
| `journey.stock_client` | 105.9 | stock client to login screen ≈ 20 + shot + quit 5 + slack 5 | 30 | 60 |
| `journey.app_desktop` (GUI, stand-in) | 367.9 | relocated copy opened, loading window and hand-off focus, three reopen cases, second open "already running", Dock quit and shutdown, Option-open Backups window, restore of a scratch backup, reopen plays | 180 | 240 |
| **Journeys total** | 1437.5 | | **575** | 810 (≤ 12 min) |

**Complete acceptance suite:**
- With the pool: 1200.7 + 575 = **1775.7 s (29.6 min)**, which meets the 30 min target.
- Serial: 1368.4 + 575 = **1943.4 s (32.4 min)**, a miss.
- So the pool, or the fallback levers, are part of the plan, not optional.

**Example pr runs.** These include the unit layer (46 s) and selection (5 s), and exclude build time.

The shared-server overhead when any shared test is selected is fixture start 24.3 + smoke 15 + down 5.9 + guards 4 = 49.2 s. With unit and selection, that is 100.2 s.

| PR shape | Arithmetic (s) | Minutes |
|---|---|---:|
| Saves feature (MoongladeSaves.cpp, add-on Lua, `charsaves.py`) | 100.2 + saves exclusive (`atomic` 18.6 + `scope` 50 + `load_ordering` 90 + `table_coverage` 0.1) 158.7 + saves pooled 162.2 / 2 = 81.1 + savesctl 31.8 + J3 110 + J1 115 = 596.8 | 9.9 |
| Pause feature (MoongladePause.cpp, add-on Lua) | 100.2 + pause 198.8 + J2 55 + J1 115 = 469.0 | 7.8 |
| Launcher (`launcher.m`, `app.py`) | 51 + `app.startup_failure` 11 + 49.2 + `loading_stages_warm` 25 + J6 180 + J1 115 = 431.2 | 7.2 |
| Play loop (`play.py`) | 51 + cold 305.5 + 49.2 + savesctl hygiene 25 + app warm 25 + config gate 33 + J1 115 + J6 180 = 783.7 | 13.1 |
| New feature with ≤ 2 min of new tests | 100.2 + 120 + J1 115 = 335.2 | 5.6 |
| FULL trigger | 51 + 1775.7 = 1826.7 | 30.4 |
| Docs only | 46 + 5 | 0.9 |

### 9.5 The 15 min mean and what would close the gap
On the #19–#33 mix the projected mean is **22.2 min** (§8), a miss. Feature PRs alone average 9.9 min. The options, with the replay mean each would give:
| Option | FULL time | Replay mean |
|---|---|---:|
| As proposed | 30.4 | 22.2 |
| + lane B runs the shared segment while lane A runs the journeys (needs ≥ 5 GB free; not guaranteed) | 0.9 unit + 5.3 cold + max(lane B: 0.4 start + 14.7 pooled shared, lane A: 0.4 start + 9.6 journeys) ≈ 21.3 | (9 × 21.3 + 59.6) / 15 ≈ 16.8 |
| + FULL-trigger PRs run the complete integration layer, `first_play` and the journeys that assert on the touched paths, with all six journeys only at gates (needs Zach's OK: it narrows "complete suite on core changes") | 20.0 + 1.9 + ≈ 1.5 + 0.9 ≈ 24.3 | (9 × 24.3 + 59.6) / 15 ≈ 18.6 |
| both | ≈ 21.3 (lane B's shared segment dominates) | ≈ 16.8 |

None of the options alone gets the platform-heavy mix under 15 min. Feature-heavy stretches (at most 25 % FULL) do. `report.pr_average` makes a drift visible as a FAIL. A miss lets the PR merge only with a STATUS.md process item and a named remedy (spec). That way a stretch of platform work doesn't deadlock merges, and the miss is never reported as a pass.

## 10. Journeys
Each is a registered check with `layer="journey"`, `budget_s` and `covers`. Shared mechanics:
- one scratch play client per run, built fresh for `first_play` and reused (WTF reset) by the others;
- the harness test account;
- `PlayRun` as today;
- the glue test hook for scripted actions.

| Journey | Asserts (the scenarios that point at it) | `covers` (non-Python) |
|---|---|---|
| `journey.first_play` | `app.launch_lifecycle`, `app.loading_stages`, `app.shutdown_window`, `login.auto_login`, `login.fresh_menu_waits`, `login.no_glue_errors`, `login.loading_overlay`, `login.overlay_progress`, `login.charselect_layout`, `login.charselect_wording`, `client.wording_addon`, `client.first_start_settings`, the written resolution (`client.resolution_default`'s live half), the backup-on-quit stage, graceful halt, nothing left running | `play`, `harness/app/*`, `client-mods/**`, `patches/client/**` |
| `journey.pause` | `client.pause_menu` | `client-mods/addons/Moonglade/*`, `modules/mod-moonglade/src/MoongladePause.cpp`, `src/logic/Pause*` |
| `journey.saves` | `saves.live_charselect_load`, `saves.live_charselect_list`, `savesctl.live_roundtrip`, `savesctl.live_codes`, `savesctl.live_down`, `savesctl.realm_restored`, `savesctl.account_name_kept` (live half) | `client-mods/glue/HSCharSaves.lua`, `client-mods/glue/HSFrontMenu.*`, `client-mods/addons/Moonglade/MoongladeSaves.lua`, `modules/mod-moonglade/src/MoongladeSaves.*`, `src/logic/Saves*` |
| `journey.login_recovery` | `login.friendly_errors`, `login.overlay_progress_reload`, `login.cancel_shows_list` (10 s live half) | `client-mods/glue/HSSeamless.lua`, `client-mods/glue/HSFrontMenu.*` |
| `journey.stock_client` | `client.stock_mode` (stock half) | `patches/client/**` (the stock path must ignore them) |
| `journey.app_desktop` (GUI) | `app.names_running`, `app.single_dock_icon`, `app.handoff_focus`, `app.reopen_*`, `app.single_instance`, `app.relocatable`, `app.shim_scope`, `app.backups_window` | `harness/app/*`, `branding/*` |

Two journey slots are left. The first M4 candidate is a dungeon run with companions. Adding it is a spec change, and it must fit the 12 min total.

## 11. Migration order
Each slice moves one area, done the same way every time:
1. Measure the area today (its rows in appendix A; for areas with shared sessions, the session total).
2. Write the unit tests, the integration tests and the journey assertions.
3. Prove each new test fails on a counterexample.
4. Re-point the capability spec's `Check:` lines (a MODIFIED delta in this change, plus a spec-change log entry; scenario text unchanged unless the entry says so).
5. Delete the old checks.
6. Run the area's tests 3×.
7. Record the before and after times in the proposal's log.

The order follows the cost and the dependencies:
1. **backups** (9.4 min).
2. **saves + savesctl** (12.2 min; first C++ extraction, first Lua add-on tests, `journey.saves`).
3. **client** (9.1 min; `journey.stock_client`, drop `client.launch_smoke`).
4. **pause** (8.8 min; `PauseLogic`, the freeze matrix, `journey.pause`).
5. **app** (8.6 min; `journey.app_desktop`, which also takes `app.backups_window` from slice 1).
6. **login** (7.1 min; the Lua front-menu suite, `journey.first_play`, `journey.login_recovery`).
7. **play** (6.1 min; S1–S3).
8. **The rest:** lifecycle, control, fixtures, era (restart merge), comp, core, lane, report (drop `report.tiers`' nested run), build, firstlaunch, release, signing, power, re, public, repo, setup.
9. **Dungeons**, after m4-dungeon-scaling merges: `DungeonsLogic` and its checks placed in layers. Its first PR is also the first real test of "a new feature adds ≤ 2 min".

The engine (tasks 2.x) lands before slice 1. Until a slice migrates an area, the area's old checks run unchanged as `integration` (`stack="up"`) or as cold-segment tests (`stack="down"`), so every interim state is a working harness.

## 12. As built
(Filled in by the builders: measured per-test and per-layer times, and any deviation from this design with its reason.)

### 12.1 Go/no-go measurements (tasks 1.1–1.4, 2026-10-11, this Mac, prototype commit on `change/test-pyramid`)
**Verdict: NO-GO as designed.** 1.1 passes. 1.2 fails one criterion (per-test time over projection + 20 %) and is
incomplete (the pause tests were not ported). 1.3 shows the per-PR mean far above 15 min, and the complete suite above
30 min. Details below; nothing was migrated.

**1.1 Unit runners: PASS.**
| What | Measured |
|---|---|
| Python unit tests (20) | 0.70–0.73 s for the suite; slowest test 0.058 s; ≈ 33 ms per test, mostly `UnitCase`'s player-file fingerprint (taken twice per test) |
| C++ tests (10, `mg_unit`) | run 0.004 s; build cold 1.42 s (clang++, `-isysroot`), cached 0 s (input SHA-256 stamp) |
| Lua tests (5, Lua 5.1.5) | run 0.016 s; slowest 0.0044 s (the simulated 60 s no-login: 600 × `advance(0.1)`); Lua build 2.39 s once; XML prelude 0.004 s |
| Static unit-layer checks (51 of appendix A's unit rows, minus the 10 rewritten as true unit tests and `login.backdrop_choice`, which is a live check today) | serial 84.6 s; 4-process pool 24.0 s (slowest `report.keep_awake` 9.3 s) |
| Projected unit layer, warm | pool 24 + 400 Python tests × 33 ms ≈ 13 + C++ 1 + Lua 1 + rewritten ≈ 5 + startup 3 = **≈ 47 s** (≤ 60 s). Serial static checks would make it ≈ 107 s |
| Each test ≤ 2 s | yes: max 0.058 s (Python), 0.00002 s (C++), 0.0044 s (Lua) |
| Server build picks up `src/logic/` | yes: an incremental `hs build` (3 min 0 s wall, cmake re-glob) compiled `modules/mod-moonglade/src/logic/SavesLogic.cpp` (`build/compile_commands.json`, build.log line "Building CXX object modules/CMakeFiles/modules.dir/mod-moonglade/src/logic/SavesLogic.cpp.o"); 7 `MoongladeLogic::` symbols in `server/bin/worldserver` |

Deviations and findings:
- Lua is built with `make posix CC="cc -isysroot <SDK>"`, not `make macosx`: macosx links readline for the interactive prompt, which the runner never uses, and this Mac's stray `/usr/local/include` shadows the SDK's headers without `-isysroot` (as in app.py and crashtrace.py). The pin: `lua51_sha256=2640fc56…695333`, computed from a fresh download (221213 bytes) and equal to lua.org/ftp's published sha256.
- Static checks in the pool: `app.bundle_valid` failed only in the pool ("the blocked write isn't in the denial log"): it isn't `parallel_safe`. Eight more static checks fail outside a real `hs run` in both modes (they read run state: `report.keep_awake`, `release.player_data_untouched`'s bracket plan, `lane.refusal`, `release.test_switches_refused`, `release.mode_resolution`, `firstlaunch.preflight`) or the build stamp (`core.patch_applied`, `core.playerbots_patches_applied`: the incremental build above replaced the from-empty stamp; `hs build --clean` restores it). Their times are representative; their statuses need the run context of task 2.1.
- `CleanLabel` drops a complete multi-byte character that ends exactly at byte 48 (it pops the continuation byte, then the lead byte). Behaviour unchanged by the extraction; the unit test asserts the documented invariants (prefix, ≤ 48 bytes, whole characters). A follow-up, not part of this change.
- The Lua stub caught two load-order needs on first contact (`CharacterSelect` as a parent frame, `CURRENT_GLUE_SCREEN` from GlueParent.lua's `SetGlueScreen`); the "no login for 60 s" test fails as it should when a scripted Continue at 30 s is injected (counterexample run).

**1.2 Shared-server slice: FAIL (one criterion), incomplete.**
Ported: the fixture (one `hs up`; per-test identity = account `HSI<base36>` + two characters; reset round with leak detection; batch cleanup) and four saves tests as character-scoped (`saves.manual`, `saves.undo`, `saves.inworld_load`, `saves.item_ids_fresh`, under `saves.as_identity`). Not done: `pause.freeze_matrix`, `pause.disconnect_unpauses` with a per-test character (companion.py has no identity parameter yet), the scratch-spawn respawn test, and the fallback levers.
| Criterion | Result |
|---|---|
| Identical statuses (3× serial, 3× 2-worker pool, 1× reversed) | **PASS**: 28 of 28 pass, no leaked state, cleanup left 0 `HSI%` accounts |
| Reset median ≤ 1 s, max ≤ 3 s | **PASS**: 30 idle samples median 0.057 s, max 0.078 s; across the 7 runs median 0.057–0.064 s, max 0.073 s |
| Each ported test ≤ its 9.3 projection + 20 % | **FAIL**: manual 6.5 s (≤ 8.5 ✓), undo 8.5 s (≤ 4.2 ✗), inworld_load 22.4 s serial / 27.3 s pooled or reversed (≤ 21.2 ✗), item_ids_fresh 15.4 s (≤ 13.0 ✗). The cause is the per-test identity: 4.6 s each (account + 2 characters 0.35 s, plus one login/logout per character with 10 000 copper, which fresh characters need: `characters.zone` is 0 until the first login, and a mail needs postage), against the ≈ 1 s design 6 assumed. Without it, three of four fit (manual 1.9, undo 3.9, item 10.8); inworld_load pooled is still 22.7 s |
| Pool saving recorded | serial 53.1–53.6 s, pooled 33.7–33.8 s: **−36.6 %** on this slice (design assumed −50 % on pooled tests) |

Findings that change the design:
- Character names allow letters only, and never one letter three times in a row (ObjectMgr::CheckPlayerName → isValidString(…, numericOrSpace=false), ObjectMgr.cpp:9280–9306 at core 7f12e89). `HSI<base36 counter>` (design 6) works for accounts only; characters use `Hs` + letters.
- `saves.load_atomic` flips a server-global test switch (`.moonglade save test failafter`), so it is exclusive, not character-scoped as design 9.3's pooled list has it.
- Respawn: the pinned core's command is `.npc set spawntime <secs>` on the selected creature (cs_npc.cpp HandleNpcSetSpawnTimeCommand at 7f12e89). It **writes the world database** (`WORLD_UPD_CREATURE_SPAWN_TIME_SECS`) as well as the live creature, so it needs a scratch spawn and an undo; not run here (no ad-hoc world writes).
- `.reload config` does re-read `Moonglade.Pause.Enable`, by source: cs_reload.cpp HandleReloadConfigCommand → World::LoadConfigSettings(true) → ConfigMgr::Reload (Config.cpp:470–485, which reloads module configs) → ScriptMgr::OnAfterConfigLoad(true) (World.cpp:301) → MoongladePause.cpp:207–213. Not confirmed live (it needs a conf write mid-run, which is the profile mechanism's job in 3.4).

**1.3 Selector and replay: selection PASS (≤ 10 s); the replay mean is a large miss.**
- Selection time: 0.14–0.18 s per PR; building the plan and the 37 check-file closures once: ≈ 0.4–0.9 s; the whole 15-PR replay 3.2 s.
- Priced with the last full run (`harness/out/last-full-run`, 2026-10-11 01:01 UTC, d123522) plus design 9.3/9.4's projections; unit layer 48 s (measured, 12.1).
- **The import closures select almost everything.** 31 of 37 check files reach 60–64 modules, because `stack.py` imports `lanestack` → `play` → `lanes` → `runner` → … inside functions. Any `harness/hslib` change except a leaf selects ~130–142 tests, so non-FULL PRs (#23, #29, #30, #31) still price at 34–39 min. With module-level imports only (not fail-safe; for scale) closures are 4–26 modules, but more paths become unmapped and the FULL share rises to 80 %.
- **The complete suite is 38.2 min, not 29.6.** Integration 1715.9 s + journeys 575 s. Without the m4-dungeon-scaling checks (`scale.*`, `lock.*`: 519.5 s at their last-run times, not in the design's projection) it is 1196.4 s + 575 s = 29.5 min, which matches design 9.4.

| PR | FULL? (first reason) | Tests selected | Strict FULL (min) | Option 2 (min) | Design-rule journeys, strict |
|---|---|---:|---:|---:|---:|
| #19 save-backups | FULL (`stack.py`) | 142 | 39.0 | 34.3 | 39.0 |
| #20 seamless-experience | FULL (`config.py` not constants-only; `probe/`) | 142 | 39.0 | 39.0 | 39.0 |
| #21 easy-setup | FULL (`bootstrap`, `fork.lock`, `build.py`) | 132 | 39.0 | 32.2 | 39.0 |
| #22 seamless-polish | no | 15 | 11.8 | 11.8 | 10.8 |
| #23 test-loop-speedups | no (closures) | 142 | 39.0 | 39.0 | 31.3 |
| #24 power-guard | FULL (`build.py`) | 132 | 39.0 | 31.3 | 39.0 |
| #25 second-stack | FULL (`config.py`/`paths.py`; `lane.py` unmapped) | 141 | 39.0 | 31.3 | 39.0 |
| #26 go-public | FULL (deleted `tools/rename-folder.sh` unmapped) | 133 | 39.0 | 31.3 | 39.0 |
| #27 release-build | FULL (`fork.lock`) | 133 | 39.0 | 31.3 | 39.0 |
| #28 release-first-launch | FULL (`config.py`, `paths.py`) | 142 | 39.0 | 34.3 | 39.0 |
| #29 release-signing | no (closures) | 142 | 39.0 | 39.0 | 34.3 |
| #30 icon-corner-c | no (closures) | 132 | 38.2 | 38.2 | 33.5 |
| #31 saves-channel | no (closures) | 132 | 38.2 | 38.2 | 33.7 |
| #32 true-pause | FULL (`patches/core`) | 68 | 39.0 | 32.2 | 39.0 |
| #33 front-menu-saves | FULL (`config.py` not constants-only) | 142 | 39.0 | 36.0 | 39.0 |
| **Mean** | 10 of 15 FULL (67 %) | | **37.1** | **33.3** | 35.6 (option 2: 31.8) |

Minutes are unit + selection + integration + journeys; gate checks are excluded as the budget defines them (with them, strict 48.9, option 2 45.1). "Option 2" = the proposal's open question 1 (b): a FULL PR runs the complete integration layer, `journey.first_play` and only the journeys whose covers match its paths. "Design-rule journeys" = design 8's narrower journey rule (a journey whose closure merely contains the path is replaced by `first_play`); the spec's rule 3 and its scenario (f) select those journeys too. Compared with design 8's rough table (mean 22.2, 9 of 15 FULL): FULL share is similar (10 of 15; #23 is no longer FULL, #25/#26/#33 are), but the non-FULL PRs are ~38 min instead of 4–16, and FULL is 39 min instead of 30.4.

Prototype covers (selector.py `FILE_COVERS`) are by check file; `harness/hslib/cli.py` and `camask.py` were added after the first replay showed them unmapped (they are run as subprocesses, so no import closure reaches them).

**What would change the outcome** (for the orchestrator and Zach; none tried here):
1. Break the import coupling (stack.py → lanestack → play → lanes → runner) so closures shrink, or select by declared `covers` plus module-level imports with a reviewed fail-safe for lazy imports.
2. Fit the dungeon checks into the 30 min (≈ 8.7 min today) or move most of them to unit tests (DungeonsLogic) and the gate.
3. Pre-create per-test identities in bulk (in parallel, once per run) instead of 4.6 s per test.

### 12.2 Part 1 as built (engine 2.x, slices for pause, saves + savesctl, dungeons, backups unit; 2026-10-11)
Measured on this Mac, one lane. "Before" is the last full run (`d123522`, 2026-10-11 01:01 UTC); "after" is the
combined run of the part-1 tests (run 2, 14 min 46 s wall, one `hs up`, four profile restarts), with the per-area
runs and the counterexample run as the other repeats.

| Area | Before (s) | After (s) | What changed |
|---|---:|---:|---|
| pause, integration | 532.9 (10 checks, own sessions) | 136.6 | `pause.freeze_matrix` 41.4 (5 scenarios, one 20 s pause); `respawn_frozen` 50.9 (scratch spawn, 5 s respawn, pause-respawn profile, R + 3 s and R + 5 s pauses); `disconnect_unpauses` 4.5 (own identity, no 90 s offline wait); `disabled_noop` 5.5 (+ the pause-off group restart); taxi 11.6, logout 22.7 unchanged |
| pause, journey | 83.6 (cold `client.pause_menu`) | 73.7 (warm `journey.pause`, budget 90) | `HS_PLAY_KEEP_STACK` on the shared stack; GIVEN nothing else running, afterwards nothing but the stack, same pids |
| pause, gate | skipped (slow tier) | 322.4 | `pause.session_alive` (5 min soak), gate layer |
| saves + savesctl, integration | 435.1 | 392.4 | `saves.session_a` 20.9 pooled with `session_b` 78.0; `session_interval` 93.8 (exclusive: it shortens the server-wide interval); `scope` 66.7 and `load_ordering` 90.6 on their own identities; atomic 18.5, load_atomic 3.5, hygiene 40.0 unchanged |
| saves + savesctl, unit | (integration before) | 4.5 | `savesctl.load_verb`, `savesctl.build_refused`, `saves.addon_static` |
| dungeons (scale + lockouts in sessions) | 349.9 | 167.8 | `dng.session1` 83.1 and `dng.session2` 65.2 (budget 90 each, enforced by `report.layer_budgets`); `scale.disabled_stock` 11.9 and `scale.outdoor_untouched` 7.6 without their own restarts |
| backups | 43.3 (`backups.rotation`) | 3.2 (unit check, dump faked in-process) + 18 Python unit tests in 1.1 s | the rest of the area is still cold (part 2) |
| **Part-1 integration layer (run 2)** | 1317.8 | **800.5** (incl. `hs up`, fixtures 4.7 s, 4 profile restarts) | ratio 0.61 (design 13.1 assumed 0.433) |

- Unit layer (`hs test`, 4-process pool): 38.5–49.9 s wall; Python 35 tests ≈ 1.1 s, C++ 27 tests (build 2.4 s when its inputs change), Lua 5 tests.
- Fixture: run-start phase 4.6–4.7 s (identities in parallel; world facts from the cross-run cache: 0.23–0.26 s, keyed by the world DB's content hash; 4.55 s on a cold cache, design 14's ≤ 10 s); resets median 0.064–0.072 s, max 0.084–0.112 s; no leaked state in any run; cleanup 0.2–0.7 s, no stale `HSI%` accounts left.
- Counterexamples (one run, sabotage reverted): each new or changed test failed on its own scenario (the five freeze children, respawn, disconnect, the disabled profile, `journey.pause`, the three saves sessions, `scope`, `load_ordering`, `scale.pause_safe` in session 1, `lock.fresh_after_clear` in session 2 with `lock.partial_kept_dismissed` blocked by it); unit: Python (backups manifest, refusals, notice), C++ (PauseLogic, DungeonsLogic), `backups.rotation` with `KEEP` changed.
- Deviations from this design, with the reason:
  - `dng.session2` order: `lock.partial_kept` (two companions, leave, re-enter: same copy), then `lock.fresh_after_clear` (the same two companions clear the copy: new copy B, both in B), then `lock.partial_kept_dismissed` in B (dismiss both, re-enter alone: same B). Each scenario has its own observation; no companion is re-added (addclass bots aren't re-addable by name), so `fresh_after_clear` asserts both companions in the new copy as its spec says (design 14).
  - `pause.respawn_frozen` runs in its own profile group `pause-respawn` (5 s normal corpse decay) so the respawn remainder R stays at most 15 s with a 5 s scratch respawn.
  - `dng.session1/2` run in a `scaling-on` profile group, after `scaling-off`, so the unscaled reference is read once in the same run.
  - Shared-world hazard found: `saves.prepare_rich` tamed the nearest wolf to the start point, which an earlier test on the shared server may have killed; a dead (or refused) candidate now moves to the rule's next one.
  - Legacy client checks (not migrated) that start the real client are counted as journey time (`runner.LEGACY_CLIENT`), including the live saves/savesctl checks, `login.backdrop_choice` and `client.resolution_default`.
- Not done in part 1 (part 2): `journey.saves` (the live savesctl and character-screen checks are still legacy client checks, 290 s), the rest of `SavesLogic`, the Lua add-on tests (saves and pause), backups kill-safe/restore on the shared server, the remaining slices 3.3, 3.5–3.8, and the `PENDING_SLEEPS` entries of the part-1 areas (reported by `report.no_fixed_sleeps`, not failed).
- Levers not taken yet: `saves.load_ordering` is character-scoped and could share the pool with `session_b` (−57 s); the four profile restarts (≈ 60 s) could shrink if `pause-respawn` folded into another group.

### 12.3 Part 2 as built (2026-10-11; journeys, part-2 slices, fixes) — the budgets are NOT met
Measured on this Mac, one lane (lane B refused: 3.88 GB available < 5.0 GB). "Before" is the branch's pr run at
`e20f3b2` (2026-10-11 08:24 UTC); "after" is the full run at `3c95011` (2026-10-11 13:31–15:59 UTC, wall 8887.9 s).

| Layer | Before (s) | After (s) | Budget (s) | Verdict |
|---|---:|---:|---:|---|
| Unit layer (`hs test`, whole tier wall 50.5 s) | 38.9 | 39.0–39.3 | 120 | PASS |
| Integration (union of spans) | 2567.3 | 2321.4 | 900 | **FAIL** (+1421 s) |
| Journeys (the six + `client.launch_smoke`) | 1526.0 | 1175.4 | 810 (sum of budgets) | **FAIL** |
| Acceptance (integration + journeys) | 4093.3 (68.2 min) | 3496.8 (58.3 min) | 1800 | **FAIL** (+28.3 min) |
| Gate (timed apart) | 6084.0 | 5328.6 | — | reported |

Per journey (budget): `journey.pause` 76.4 (90) pass; `journey.stock_client` 32.1 (60) pass; `journey.login_recovery`
151.2 (120) miss; `journey.saves` 194.6 (150) miss; `journey.first_play` 229.1 (150) miss; `journey.app_desktop` 453.5
(240) miss.

Per area, integration (timeline spans; before → after, s): backups 522.3 → 516.9 (kill-safe/restore not migrated);
play 360.3 → 379.2 (S1–S3 not merged); saves 380.6 → 367.7; dungeons (lock/dng/scale) 334.1 → 332.5; client 194.4 →
194.8 (includes the run guard `client.player_client_not_rebuilt` 46.9 and `client.launch_smoke` 38.6, which no spec
delta removes); pause 138.1 → 137.3; fixture start and profile restarts 83.7 → 106.0 (+ one extra stack stop/start:
with lane B refused its `play.*` checks ran inside the shared segment; fixed after this run in `f73c320`, not yet
measured); comp 81 → 81.7; era 114.6 → 79.3 (one off/on restart pair for both scenarios); lifecycle 63 → 57.2;
fixture self-tests 152.1 → 0 (gate); lane.merge + lane.preflight 74.7 → 0 (gate); app 52.7 → 52.6; savesctl 40 → 41.3.

What changed in part 2:
- **Journeys.** The six named journeys exist (`checks/journey_checks.py`; `journey.pause` from part 1); every legacy
  client/app scenario is a child of one of them, with its own record and its existing `Check:` id. Warm journeys
  (`stock_client`, `login_recovery`, `saves`) run with `HS_PLAY_KEEP_STACK` on the shared stack (`play_checks.WARM`:
  "nothing left running" = nothing besides the same stack, same pids; a deliberate login-server restart is recorded).
  `first_play` runs its cold session first (./play starts and stops the stack), then one `hs up` for its other sessions,
  then `hs down`. `app_desktop` stays cold (the app refuses the keep-stack switch, harness-control spec).
  Deviation: `app.launch_lifecycle`, `app.loading_stages` and `app.shutdown_window` are in `app_desktop` (they share its
  app session), not in `first_play` as design 10's table has them; `login.overlay_progress_reload` is in `first_play`
  (it asserts on that cold session's Main Menu -> Continue reload), not `login_recovery`.
  Not built: the merged single sessions design 9.4 projects (one cold app open for `first_play`, one session for
  `saves`, one GUI session for `app_desktop`); each journey still runs its scenarios' former sessions back to back.
- **Live waits.** `login.cancel_shows_list`: 10 s live + the simulated 60 s Lua test run inside the scenario
  (spec-change log 2026-10-10). `era.toggle_roundtrip` + `era.scope_guard`: one restart pair (`era.session_toggle`,
  generators driven in lockstep). `saves.load_ordering`: its WorkerThreads = 2 check by a worldserver restart.
- **Unit layer.** Lua add-on tests on an in-game stub (`addonstub.lua`): 9 pause (menu hooks, banner, dim, strata,
  Logout/Quit resume, late answers) and 12 saves (window, protocol, refusals, paging, default keys, pause kept);
  `SavesLogic` gained JsonEscape, IsHexType/IsFloatType, SqlValue, FormatTime, DefaultLabel, SaveRefusal and
  DisabledReason (6 C++ tests), called by MoongladeSaves.cpp; Python tests for the runner/fixture fixes, the era driver,
  warm journeys, the lane fallback order and the history scan's email rule. Unit layer: Python 50, C++ 33, Lua 26.
- **Fixes.** No reset round after `lifecycle.down` (it read the stopped server as leaked state and restarted the world
  before `first_play`); the leak wording names an interval change only when the interval changed; the release bracket
  covers the release.*/firstlaunch.* checks the acceptance segments run (unit-layer ones never reach its log);
  `report.layer_budgets`' "new selected checks" counts acceptance checks only (design 9.1); the summoned final boss of
  LFG 136 is read by GUID with the selection re-set before each command (the self-AI changes it); the history dry
  run plants its own self-test commit and the scan ignores image scale suffixes (`icon_512x512@2x.png`);
  lane.merge, lane.preflight and fixture.* are gate checks (real-stack harness self-tests); lane.preflight's forced
  lane-B set is two acceptance checks inside lane B's window; with lane B refused, its stack-down checks run after
  `hs down`.
- Not done in part 2: backups kill-safe/restore on the shared server; play S1 merge and S2/S3 trims; the config-gate
  warm run; HSCharSaves.lua / HSSeamless.lua glue tests beyond part 1's front-menu set; `ListGen` and the
  `ReadSave`/`CheckCharacter` validation in `SavesLogic`; `PENDING_SLEEPS` (69 waits still reported); the passive
  observer for `client.login_text_clean`; pooling `saves.load_ordering`.

Where the remaining acceptance time goes (after): integration 2321 s = backups 517, play 379, saves 368, dungeons
333, client 195, pause 137, fixture/profile restarts 106, comp 82, era 79, lifecycle 57, app 53, savesctl 41;
journeys 1175 s = app_desktop 454, first_play 229, saves 195, login_recovery 151, pause 76, launch_smoke 39,
stock_client 32. The cold `./play` sessions (backups, play, app_desktop, first_play's cold half) are about 1350 s.

## Appendix A: every record of the baseline run, where it goes
`Today` is the baseline `duration_s`. A 0.0 is either a shared session whose time sits on its first check, or a skipped slow check. `Goes to` uses the layers in §2; `J1`–`J6` are, in order, `first_play`, `pause`, `saves`, `login_recovery`, `stock_client` and `app_desktop`.

| Check | Today (s) | Tier today | Goes to | Why |
|---|---:|---|---|---|
| `app.icon` | 6.0 | quick | unit | static image geometry |
| `app.bundle_valid` | 6.7 | quick | unit | builds the bundle in a sandbox, no stack |
| `app.names_running` | 39.5 | full | J6 | gui |
| `app.icon_everywhere` | 2.1 | quick | unit | static |
| `app.launch_lifecycle` | 39.8 | full | J1 | J1 opens the app cold and quits from the Dock |
| `app.startup_failure` | 10.9 | full | integration (own ./play) | fails before the client; cheap |
| `app.single_instance` | 82.2 | full | J6 | second open during J6 |
| `app.relocatable` | 41.0 | full | J6 | J6 runs from a relocated copy |
| `app.backups_window` | 155.7 | full | J6 | J6 ends with Option-open -> Backups -> restore |
| `app.single_dock_icon` | 49.4 | full | J6 | gui |
| `app.handoff_focus` | 0.0 | full | J6 | gui |
| `app.reopen_focuses_game` | 0.0 | full | J6 | gui |
| `app.reopen_during_loading` | 0.0 | full | J6 | gui |
| `app.reopen_unminimizes` | 0.0 | full | J6 | gui |
| `app.loading_stages` | 0.0 | full | J1 | J1's stage events |
| `app.shutdown_window` | 45.7 | full | J1 | J1's quit |
| `app.dock_icon_rendered` | 0.0 | slow | gate | slow tier today |
| `app.shim_scope` | 0.0 | full | J6 | asserted during J6 |
| `app.loading_stages_warm` | 36.8 | full | integration | warm ./play on the shared server |
| `app.shim_perf` | 0.0 | slow | gate | perf soak |
| `backups.on_quit` | 71.1 | full | integration (own ./play) | one stand-in session: backup ordering on quit |
| `backups.rotation` | 43.2 | full | unit | rotation over fake backups in a temp folder (43 s -> <1 s) |
| `backups.failure_nonblocking` | 77.7 | full | integration (own ./play) | one injected-failure session; notice text + record as unit tests |
| `backups.kill_safe` | 166.7 | full | integration | kill `backups take`/restore directly on the shared mysqld; no ./play session |
| `backups.restore_roundtrip` | 112.3 | full | integration | restore into scratch schemas through the CLI, stack already up |
| `backups.restore_refusals` | 92.2 | full | integration | one damaged-backup case live; the other refusal reasons as unit tests |
| `backups.player_untouched` | 1.0 | quick | run guard | run-level guard |
| `build.pin_enforced` | 20.8 | full | gate | build inputs |
| `build.clean` | 246.1 | full | gate | build from empty: accepted fixed cost, runs when build inputs change |
| `build.clean_smart` | 18.2 | full | gate | build inputs |
| `build.moved_repo` | 4.9 | full | gate | build inputs |
| `build.downloads_pinned` | 1.0 | quick | unit | static |
| `build.playerbots_active` | 0.1 | quick | integration | server smoke |
| `client.realmlist` | 0.1 | quick | integration | server smoke |
| `client.protocol_probe` | 0.6 | quick | integration | server smoke |
| `client.protocol_badpass` | 5.3 | quick | integration | server smoke |
| `client.login_text_clean` | 31.8 | full | integration | probe, 30 s stay |
| `client.launch_smoke` | 38.3 | full | dropped | redundant: every journey launches the real client; J5 covers the stock exe |
| `client.config_gen` | 0.2 | quick | unit | mock tree |
| `client.inspect` | 0.0 | quick | unit | hashes |
| `client.pristine_locked` | 2.0 | quick | unit | scratch APFS clone |
| `client.build_refuses_pristine` | 1.6 | quick | unit | scratch |
| `client.patch0001_recorded` | 0.0 | quick | unit | static |
| `client.play_build` | 2.2 | quick | unit | scratch build |
| `client.play_revert` | 6.2 | quick | unit | scratch build |
| `client.resolution_gate` | 1.6 | quick | unit | gate rules |
| `client.config_gate` | 68.9 | full | integration | warm stand-in run; the gate predicate as unit tests |
| `client.real_client_refused` | 3.0 | quick | integration | refusal before start |
| `client.resolution_default` | 75.7 | full | unit | resolution decision with injected displays; J1 asserts the written value |
| `client.stock_mode` | 105.9 | full | J5 | stock run only; patched mode is J1 |
| `client.first_start_settings` | 74.8 | full | J1 | J1 builds a fresh play client |
| `client.wording_addon` | 0.1 | full | J1 | J1 |
| `client.pause_menu` | 81.8 | full | J2 | pause journey |
| `client.player_client_not_rebuilt` | 44.7 | full | run guard | run-level fingerprint guard; its probe run merged into client.config_gate's |
| `client.player_config_untouched` | 0.0 | quick | run guard | run-level guard |
| `comp.add_remove` | 6.6 | full | integration | character-scoped; playerbots |
| `comp.follows` | 29.0 | full | integration | character-scoped; playerbots |
| `comp.assists` | 69.5 | full | integration | character-scoped; playerbots |
| `comp.quest_shared` | 6.4 | full | integration | character-scoped; playerbots |
| `core.patch_applied` | 0.2 | quick | unit | git apply on a scratch index |
| `core.playerbots_patches_applied` | 0.7 | quick | unit | git apply on a scratch index |
| `core.login_logout_race` | 5.7 | full | integration | server crash guard, exclusive |
| `core.login_logout_race_baseline` | 63.6 | full | gate | rebuilds the unpatched core; selected by patches/core |
| `era.sql_deterministic` | 3.9 | quick | integration | server reads |
| `era.default_on` | 0.8 | quick | integration | server reads |
| `era.xp_curve` | 0.9 | quick | integration | server reads |
| `era.quest_xp` | 1.4 | quick | integration | server reads |
| `era.elites` | 9.9 | quick | integration | server reads |
| `era.mounts` | 5.1 | quick | integration | server reads |
| `era.toggle_roundtrip` | 48.0 | full | integration | merged restarts with era.scope_guard: 2 restarts, not 4 |
| `era.scope_guard` | 44.1 | full | integration | shares era.toggle_roundtrip's restarts |
| `firstlaunch.client_refusals` | 6.3 | full | integration | no stack; refusals |
| `firstlaunch.downloads` | 40.7 | full | gate | loopback downloads of GB files; selected by firstlaunch/downloads paths |
| `firstlaunch.flow` | 0.0 | slow | gate | slow tier today: gate layer |
| `firstlaunch.kill_resume` | 0.0 | slow | gate | slow tier today: gate layer |
| `firstlaunch.preflight` | 4.9 | quick | unit | preflight rules |
| `firstlaunch.second_open_plays` | 0.0 | slow | gate | slow tier today: gate layer |
| `firstlaunch.source_untouched` | 0.0 | slow | gate | slow tier today: gate layer |
| `fixture.account_character` | 0.5 | quick | integration | server smoke |
| `fixture.level` | 0.3 | quick | integration | server smoke |
| `fixture.teleport` | 0.3 | quick | integration | server smoke |
| `fixture.add_item` | 0.4 | quick | integration | server smoke |
| `fixture.start_quest` | 0.8 | quick | integration | server smoke |
| `fixture.instant_logout` | 1.1 | quick | integration | server smoke |
| `gm.server_info` | 0.2 | quick | integration | server smoke |
| `gm.invalid` | 0.1 | quick | integration | server smoke |
| `harness.prefix_scope` | 4.9 | full | integration | no stack; Wine start; selected by procs/launch paths |
| `lane.isolation` | 29.2 | full | gate | real lane-B stack; selected by lanes/lanestack/paths |
| `lane.independent_down` | 71.0 | full | gate | real two-stack test; selected by lanes/lanestack/stack |
| `lane.merge` | 65.4 | full | unit | nested run of stub checks instead of real ones |
| `lane.orphans` | 48.4 | full | gate | real lane-B stack + kill; selected by lanes/runreg |
| `lane.paths` | 0.2 | quick | unit | pure path table |
| `lane.preflight` | 11.7 | full | unit | decision rules already pure (lanes.preflight_decision) |
| `lane.refusal` | 3.3 | quick | unit | refusals exit before anything starts |
| `lane.table` | 0.0 | quick | unit | pure table rules |
| `lifecycle.failfast` | 6.5 | full | integration (own ./play) | fails before start; cold segment |
| `lifecycle.up` | 24.3 | quick | integration | becomes the shared fixture's own start (asserted once per run) |
| `lifecycle.idempotent` | 0.7 | quick | integration | shared server |
| `lifecycle.isolated` | 18.6 | quick | integration | shared server; lsof narrowed to our pids (18.6 s -> ~3 s) |
| `lifecycle.persist` | 25.0 | full | integration | one restart, grouped with the profile restarts |
| `lifecycle.down` | 5.9 | quick | integration | end of the shared segment (before J1) |
| `login.continue_context` | 1.0 | full | integration | shared server |
| `login.auto_login` | 79.5 | full | J1 | first-play journey |
| `login.cancel_shows_list` | 149.6 | full | J4 | Lua unit with a simulated 60 s clock + 10 s live watch in J4 (spec-change log) |
| `login.fresh_menu_waits` | 0.0 | full | J1 | J1 uses a fresh play client |
| `login.no_glue_errors` | 0.0 | full | J1 | J1 + every journey's glue log |
| `login.backdrop_choice` | 0.0 | full | unit | Lua unit (pick -> stored -> applied next start); J1 asserts the stored setting |
| `login.loading_overlay` | 74.0 | full | J1 | J1 |
| `login.charselect_layout` | 0.0 | full | J1 | J1 |
| `login.charselect_wording` | 0.0 | full | J1 | J1 |
| `login.overlay_progress` | 0.0 | full | J1 | J1; bar logic also as Lua unit tests |
| `login.overlay_progress_reload` | 0.0 | full | J4 | login-recovery journey |
| `login.friendly_errors` | 122.2 | full | J4 | login-recovery journey |
| `pause.combat_frozen` | 58.4 | full | merged into `pause.freeze_matrix` | one 20 s pause covers combat, auras, cooldowns, cast, bots |
| `pause.aura_frozen` | 46.3 | full | merged into `pause.freeze_matrix` | same pause |
| `pause.cooldown_frozen` | 40.3 | full | merged into `pause.freeze_matrix` | same pause |
| `pause.cast_cancelled` | 29.0 | full | merged into `pause.freeze_matrix` | same pause |
| `pause.respawn_frozen` | 144.6 | full | integration | scratch spawn with a short respawn time |
| `pause.bots_frozen` | 28.3 | full | merged into `pause.freeze_matrix` | same pause |
| `pause.session_alive` | 0.0 | slow | gate | 5-minute soak |
| `pause.refused_on_taxi` | 11.8 | full | integration | exclusive (world-global) |
| `pause.logout_unpauses` | 22.0 | full | integration | 20 s logout countdown is the spec |
| `pause.disabled_noop` | 60.8 | full | integration | profile restart grouped (or .reload config) |
| `pause.disconnect_unpauses` | 84.3 | full | integration | per-test character: no 90 s offline wait |
| `perf.server_budget` | 0.0 | slow | gate | slow tier today: gate layer |
| `perf.population_tuned` | 0.0 | slow | gate | slow tier today: gate layer |
| `play.player_account_untouched` | 1.3 | full | integration | fixture reset guard |
| `play.loop_clean_exit` | 38.0 | full | integration (own ./play) | play session S1 (stand-in, offline sandbox): normal exit |
| `play.interrupt_clean_exit` | 61.9 | full | integration (own ./play) | S2: SIGINT live; SIGTERM + repeated-signal handling as unit tests of the handler |
| `play.client_crash_cleanup` | 32.9 | full | integration (own ./play) | S3: SIGKILL the stand-in |
| `play.startup_failure` | 8.1 | full | integration (own ./play) | fails before the client; cheap |
| `play.single_instance` | 36.3 | full | merged into `play.loop_clean_exit` | second ./play refused while S1 runs |
| `play.offline` | 54.7 | full | merged into `play.loop_clean_exit` | S1 runs under the sandbox profile; one session instead of two |
| `play.player_account` | 93.8 | full | integration (own ./play) | one first-run session; file/permission rules as unit tests |
| `play.dock_identity` | 38.1 | full | merged into `play.loop_clean_exit` | sampled during S1 |
| `play.stage_lines` | 0.1 | full | merged into `play.loop_clean_exit` | S1's own stage lines |
| `play.world_progress` | 0.0 | full | merged into `play.loop_clean_exit` | S1's cold world start |
| `pop.size_setting` | 0.0 | slow | gate | slow tier today: gate layer |
| `pop.zones_covered` | 0.0 | slow | gate | slow tier today: gate layer |
| `pop.size_stable` | 0.0 | slow | gate | slow tier today: gate layer |
| `pop.bots_active` | 0.0 | slow | gate | slow tier today: gate layer |
| `pop.fresh_start_scope` | 0.0 | slow | gate | slow tier today: gate layer |
| `power.jobs_default` | 0.3 | quick | unit | decision rules with stubbed battery |
| `power.jobs_override` | 0.4 | quick | unit | decision rules with stubbed battery |
| `power.pause_resume` | 0.2 | quick | unit | decision rules with stubbed battery |
| `power.run_pause` | 7.5 | quick | unit | decision rules with stubbed battery |
| `power.stop_low` | 1.2 | quick | unit | decision rules with stubbed battery |
| `power.bootstrap_resume` | 0.3 | quick | unit | decision rules with stubbed battery |
| `power.switch_refusal` | 1.0 | quick | unit | decision rules with stubbed battery |
| `public.license` | 0.1 | quick | unit | static scan |
| `public.tree_scrub` | 2.5 | quick | unit | static scan |
| `public.no_game_files` | 0.1 | quick | unit | static scan |
| `public.history_dryrun` | 0.0 | slow | gate | slow tier today: gate layer |
| `re.ghidra_installed` | 0.2 | quick | unit | scratch copies |
| `re.ghidra_readonly` | 12.7 | full | gate | RE tooling; selected by ghidra paths |
| `re.mpq_readonly` | 4.5 | quick | unit | scratch copies |
| `re.ghidra_refuses_mismatch` | 0.1 | quick | unit | scratch copies |
| `re.mpq_write_roundtrip` | 0.8 | quick | unit | scratch copies |
| `release.dev_untouched` | 0.0 | slow | gate | slow tier today: gate layer |
| `release.no_abs_paths` | 0.0 | slow | gate | slow tier today: gate layer |
| `release.no_game_files` | 0.0 | slow | gate | slow tier today: gate layer |
| `release.pins` | 2.0 | full | integration | no stack |
| `release.runs_without_homebrew` | 0.0 | slow | gate | slow tier today: gate layer |
| `release.source_alongside` | 0.0 | slow | gate | slow tier today: gate layer |
| `release.app_readonly` | 0.0 | slow | gate | slow tier today: gate layer |
| `release.mode_resolution` | 1.2 | quick | unit | static |
| `release.readline_stub` | 4.4 | quick | unit | static |
| `release.stack_from_payload` | 0.0 | slow | gate | slow tier today: gate layer |
| `release.test_switches_refused` | 6.0 | quick | unit | static |
| `release.client_smoke` | 0.0 | slow | gate | slow tier today: gate layer |
| `release.play_standin` | 0.0 | slow | gate | slow tier today: gate layer |
| `release.app_moved` | 0.0 | slow | gate | slow tier today: gate layer |
| `release.player_data_untouched` | 4.7 | full | unit | static |
| `repo.naming` | 0.1 | quick | unit | static |
| `repo.agent_guard_hook` | 2.9 | quick | unit | static |
| `repo.portable` | 0.8 | quick | unit | static |
| `report.tiers_declared` | 0.1 | quick | unit | synthetic rules |
| `report.slow_tier_rule` | 0.0 | quick | unit | synthetic rules |
| `report.gui_tier_rule` | 1.3 | quick | unit | synthetic rules |
| `report.backups_tier_rule` | 0.5 | quick | unit | synthetic rules |
| `report.gui_preflight` | 0.5 | quick | unit | synthetic rules |
| `report.keep_awake` | 9.3 | quick | unit | pmset assertion check, no stack |
| `report.last_full_run` | 0.1 | quick | unit | synthetic rules |
| `report.nightly` | 1.4 | quick | unit | synthetic rules |
| `report.orphans_healed` | 49.8 | full | gate | harness self-test on a real killed run; selected by runreg/runner paths |
| `saves.live_charselect_load` | 170.8 | full | J3 | saves journey |
| `saves.live_charselect_list` | 0.0 | full | J3 | saves journey |
| `saves.addon_static` | 1.3 | full | unit | static files |
| `saves.table_coverage` | 0.1 | full | integration | reads the running schema |
| `saves.manual` | 7.1 | full | integration | character-scoped |
| `saves.quick_slot` | 4.0 | full | unit | retention rule (C++ logic) |
| `saves.refused_in_combat` | 24.0 | full | integration | one combat case live; dead/combat predicate as C++ unit tests |
| `saves.autosave_triggers` | 50.7 | full | integration | character-scoped |
| `saves.bots_skip` | 21.9 | full | integration | character-scoped |
| `saves.atomic` | 18.6 | full | integration | worldserver kill; exclusive |
| `saves.roundtrip` | 27.2 | full | integration | character-scoped |
| `saves.scope` | 66.8 | full | integration | whole-DB checksums; exclusive; 2 snapshots not 3 |
| `saves.item_ids_fresh` | 10.8 | full | integration | character-scoped |
| `saves.inworld_load` | 17.7 | full | integration | character-scoped |
| `saves.load_ordering` | 90.0 | full | integration | test profile restart; exclusive |
| `saves.undo` | 3.5 | full | integration | character-scoped |
| `saves.load_atomic` | 3.3 | full | integration | character-scoped |
| `saves.load_refusals` | 24.5 | full | integration | one live refusal; each reason as a C++ unit test |
| `saves.inworld_list` | 12.3 | full | unit | list formatting (C++ logic); the live list is asserted in saves.manual's session |
| `saves.list_wording` | 1.8 | full | unit | charsaves.render + reply lines, pure |
| `saves.player_untouched` | 1.6 | full | run guard | run-level guard |
| `savesctl.listener` | 0.5 | quick | unit | fake socket |
| `savesctl.test_actions_refused` | 6.8 | full | integration | refusal exits before anything starts |
| `savesctl.load_verb` | 3.0 | full | unit | stub load action |
| `savesctl.build_refused` | 0.3 | full | unit | fake socket |
| `savesctl.hygiene` | 36.7 | full | integration | warm ./play on the shared server |
| `savesctl.live_roundtrip` | 89.4 | full | J3 | saves journey |
| `savesctl.live_codes` | 0.0 | full | J3 | saves journey |
| `savesctl.live_down` | 0.0 | full | J3 | saves journey |
| `savesctl.realm_restored` | 0.0 | full | J3 | saves journey |
| `savesctl.account_name_kept` | 35.7 | full | J3 | live half in J3; scratch Config.wtf half as unit |
| `setup.fresh_clone` | 0.0 | slow | gate | slow tier today: gate layer |
| `setup.preflight` | 5.1 | quick | unit | static/preflight |
| `signing.adhoc_signature` | 0.0 | slow | gate | slow tier today: gate layer |
| `signing.bundle_layout` | 0.0 | slow | gate | slow tier today: gate layer |
| `signing.entitlements_needed` | 0.0 | slow | gate | slow tier today: gate layer |
| `signing.gatekeeper_adhoc` | 0.0 | slow | gate | slow tier today: gate layer |
| `signing.mode_setting` | 1.5 | quick | unit | static/stub |
| `signing.notarized` | 0.0 | slow | gate | slow tier today: gate layer |
| `signing.open_instructions` | 0.7 | quick | unit | static/stub |
| `signing.seal_survives_use` | 0.0 | slow | gate | slow tier today: gate layer |
| `signing.stable_identity` | 0.0 | slow | gate | slow tier today: gate layer |
| `sql.world_populated` | 0.1 | quick | integration | server smoke |
| `sql.assert_selftest` | 0.1 | quick | integration | server smoke |
| `report.coverage` | 0.0 | post | run guard | post-run report |
| `report.schema` | 0.0 | post | run guard | post-run report |
| `report.full_run` | 0.0 | post | run guard | post-run report |
| `report.tiers` | 155.9 | post | dropped | nested quick run removed; report.layer_budgets reads this run's own unit-layer record |
| `report.client_pristine` | 0.8 | post | run guard | post-run report |
| `report.lanes` | 0.0 | post | run guard | post-run report |
| `report.timing` | 0.0 | post | run guard | post-run report |

## 13. Redesign after the 1.4 no-go (orchestrator, 2026-10-11)

The prototype showed the plan *moved* tests between layers but didn't *reduce* acceptance work enough, and that selection by Python import closure selects nearly everything. Changes, to be re-measured before any migration (tasks 1.5–1.7):

1. **Selection by declared ownership, not import closure.** Every acceptance test declares the product paths it covers (module sources, add-on/glue files, launcher, config sections) in a small `covers=` list. Harness-library changes are covered by the unit layer plus the acceptance tests that *declare* that library; they no longer pull in everything. Fail-safe: a path nobody declares selects the **smoke set** (the launch-to-world journey plus one integration test per area), not the whole suite. A `report.ownership` unit check fails when a product path has no owner.
2. **FULL only for true foundations:** `patches/core/`, `patches/playerbots/`, `fork.lock` pins of the core or bots, and the build system. Harness, probe, stack and lane code are tested by unit tests and the smoke set. Release/signing paths select the release checks only.
3. **One integration test per feature behaviour cluster, not per scenario.** A scenario keeps its own `Check:` id, but several scenarios are asserted inside one shared session (for example, saves: manual, quick, undo and load in one character session; lockouts: fresh-after-clear, partial-kept and dismissed in one dungeon session). Budget: an integration session is at most 60 s, and the whole integration layer is at most 12 min serial.
4. **Bulk fixtures:** all per-test accounts and characters are created once, in parallel, at the start of a run (target ≤10 s total), so per-test setup cost leaves the per-test time.
5. **Dungeon checks** (519 s today): one `dng.session` integration run covers scale + lock scenarios in at most 3 dungeon entries. Boss fights stay gate-only (`scale.solo_final_boss`).
6. **Pool only what is proven safe.** Tests that flip server-wide switches (`saves.load_atomic`, pause enable) run serially; the pool saving is taken as measured (−37%), not assumed.

Projected after the redesign (to be measured, not trusted): unit ≈47 s; integration ≤12 min; journeys ≈9.6 min; complete suite ≤22 min; typical feature PR ≈8–12 min; foundation PR ≈22 min; replay mean ≤15 min with the narrower FULL rule.

### 13.1 Re-measure after the redesign (tasks 1.5–1.7, 2026-10-11)
**Verdict: the complete suite and the replay mean now fit, but three criteria of 1.6 miss**: the `dng.session` takes
142 s (budget 60 s); the run-start fixture phase takes 14.3 s (budget 10 s) once the world-facts query it needs is
counted; and the first round of runs was not stable (fixed, then 7 of 7 stable). The integration layer projects to
14.6 min (budget 12 min). Projections for the areas not yet clustered extrapolate the two measured clusters.

**1.5 Ownership selector (prototype `harness/hslib/ownership.py`): selection PASS; replay mean PASS only with clustering.**
- How the first-cut ownership map is derived (`ownership.generate()`, reviewable):
  - acceptance tests and layers from appendix A; `scale.*`/`lock.*` form the `dng` area (the boss fight stays gate);
  - product paths: the check file's covers (`selector.FILE_COVERS`) and design 10's journey covers, minus
    `mod_moonglade_loader.cpp` (every feature passes through it: smoke set) and the module confs;
  - module confs are owned **by section**: a changed `Moonglade.Saves.*` line selects the saves area, `Moonglade.Pause.*`
    pause, `Moonglade.Dungeons.*` dng, `EraProfile.*` era; a key in no known section selects the smoke set;
  - harness libraries: the check file plus the hslib modules it imports **directly** (one hop), except shared
    infrastructure, meaning modules imported directly by ≥ 6 of the 31 acceptance check files (`registry`, `paths`, `stack`,
    `soap`, `play`, `play_checks`, `playclient_util`, `clientbuild`, `db`, `launch`, `procs`, `probe`, `fixtures`,
    `config`) plus `probe/**`, `stack`, `lanes`, `lanestack`, `runner` by rule; a shared module is still declared by the
    check file named after it (`play.py` by `play_checks.py`);
  - an undeclared path selects the smoke set: `journey.first_play` + the cheapest integration test of each of 16 areas;
  - FULL: `patches/core/**`, `patches/playerbots/**`, `harness/hslib/build.py`, CMake files, and a change to a
    `core_*`/`playerbots_*` key of fork.lock (other pins select their declared owners).
- Selection time: ≤ 0.09 s per PR; generating the map 0.02–0.4 s.
- Replay of #19–#33 and #1–#3 of the new repo (18 PRs), unit layer 48 s, the run-start fixture phase 14.3 s added to
  every run with a shared-server test, pool saving as measured. FULL share **22 %** (4 of 18: #21, #24 and #3 by `build.py`,
  #32 by `patches/core`), down from 67 %:

| Integration pricing | Strict FULL | Option 2 | Complete suite |
|---|---:|---:|---:|
| No clustering gain (old per-test projections) | 21.0 min | 20.0 min | 39.1 min |
| **Measured clustering ratio 0.433** (1.6, both clusters) | **13.5 min** | **12.5 min** | **23.0 min** |
| Design 13's 12 min target (ratio 0.384) | 12.7 min | 11.6 min | 21.3 min |

  Break-even: the mean stays ≤ 15 min while integration runs at ≤ 0.55 of its old projections, and the complete suite stays ≤ 30 min
  at ≤ 0.68. Sensitivity: #1 and #2 are docs-only (0.8 min each); without them, the 16-PR mean at 0.433 is 15.1 min.
- Per PR at 0.433, strict (option 2 where it differs): #19 12.4, #20 18.6, #21 23.8 FULL, #22 9.7, #23 8.0, #24 23.8
  FULL (16.1), #25 9.9, #26 10.1, #27 11.2, #28 20.2, #29 9.6, #30 9.2, #31 10.8, #32 23.8 FULL (20.3), #33 17.1,
  #1 0.8, #2 0.8, #3 23.8 FULL (16.1). The broad non-FULL PRs (#20, #28, #33) touch the module conf/loader, the
  add-on and several areas' libraries at once.
- Smoke set: 4.4 min with unit and fixtures.

**1.6 Clustered sessions: saves PASS; `dng.session` FAIL (142 s vs 60 s); fixtures FAIL (14.3 s vs 10 s); stable after one fix.**
Prototype `harness/hslib/clusters.py`. One run = bulk fixtures, two saves clusters (each with its own identity; serial,
or both at once in the pool), then `dng.session` (exclusive: it pauses the world). Runs: 3× serial, 3× pool, 1× reversed (dng first,
saves clusters in reverse order).
- Saves cluster (`saves.manual`, `saves.quick_slot`, `saves.inworld_load`, `saves.undo` in one identity's session):
  median 9.8 s, max 14.4 s (the old per-test projections for these four: 32.3 s; ratio 0.305). Pool: two clusters
  at once in 9.4–10.3 s each, the same as one alone.
- `dng.session` (9 scenarios, 3 Deadmines entries): **median 141.7 s, max 143.0 s** (their old times: 317.4 s;
  ratio 0.446). Per scenario (median): module_loaded 0.8, outdoor_untouched 2.1, solo_weaker 6.5, pause_safe 27.6
  (the spec's 20 s pause), counts_companions + party_stock 51.1 (adding four companions and waiting for them),
  partial_kept + partial_kept_dismissed 14.3, fresh_after_clear 31.2 (clearing every encounter). 60 s isn't reachable
  with the 20 s pause, four companions and a full clear in one session; two or three sessions of ≤ 60 s each,
  or a 150 s budget, would fit what was measured.
  Not in the session: `scale.disabled_stock` (profile group), `lock.no_hourly_cap` (six entries), `lock.heroic_*`,
  `lock.login_inside_kept`, `lock.companion_unstuck`: 202 s today, ≈ 90 s at the measured ratio.
  **Loosening to review:** in the session `lock.fresh_after_clear` doesn't assert "both companions in the new copy"
  (they were dismissed for `partial_kept_dismissed` before the clear).
- Statuses: **first round FAIL**. `saves.inworld_load` errored 7 times in 21 (1 of 6 serial, 6 of 6 pooled: "the character
  never got into combat"). Cause: companion.CREATURE_RULE's query takes 14.3 s alone (a correlated isolation test over
  every spawn on the map with no spatial index) and 33 s when two sessions run it at once, more than `saves.engage`'s 25 s
  timeout. Fix (committed): `companion.creature_candidates` caches its result per process (the spawns are static; a smaller `n` is a prefix of
  the cached list, `ORDER BY d, guid`), and the fixture phase computes it once at run start, in parallel with the identities.
  **Second round: all 13 scenario ids passed in all 7 runs** (no leaked state; resets median 0.064 s, max 0.088 s).
- Bulk fixtures: 2 identities 4.5–4.75 s; 8 in 5.2 s, 16 in 6.1 s (16 workers): within 10 s. **But the run-start phase is
  14.3 s** with the world-facts query, which runs beside the identities and dominates. To fit the 10 s, cache it across runs
  keyed by the world database's content, or make it cheaper (a bounding-box rewrite didn't help: 13.7 s, same rows).

**1.7 Go/no-go 2: complete suite PASS (projected 23.0–25.0 min ≤ 30); integration layer 14.6 min > 12 min; per-session
and fixture budgets miss (above).**
- Integration, complete: 1874.2 s of old projections × 0.433 measured = 811.5 s, plus the shared overhead 49.2 s and
  the fixture phase 14.3 s = 875 s = **14.6 min** serial (budget 12). Complete suite: unit 0.8 + integration 14.6 +
  journeys 9.6 (projected, not measured) = **25.0 min** (the replay pricer, with the measured pool saving applied:
  23.0 min). Range over the two measured cluster ratios (0.305–0.446): 21.0–25.4 min.
- Assumptions to watch: the other areas cluster as well as saves and dng did (ratio ≤ 0.55 keeps the PR mean ≤ 15 min); the
  journeys hit design 9.4's projections; and the cached world-facts query is the only shared slow query (`comp.*` uses the same rule,
  so it gains too).


## 14. Go decision after 1.7 (orchestrator, 2026-10-11)

Zach's targets are met by the measured projection: unit layer about 47 s (≤ 2 min); replay mean 12.5–13.5 min (≈ 15); complete suite 23–25 min (≤ 30). The three misses are against my own sub-budgets, which are rebalanced here without touching Zach's targets:
- **Integration layer:** ≤ 15 min serial (was 12). The complete suite stays ≤ 30 min, enforced.
- **`dng.session`:** split into two sessions of ≤ 90 s each, and companions persist across both, so their 51 s setup is paid once. `scale.pause_safe` pauses 5 s, not 20: values are compared to the millisecond, so a 5 s pause still shows any drift. The loosening found in 1.6 is **not accepted**. `lock.fresh_after_clear` must again assert that both companions are in the new copy, by re-summoning them in the second session.
- **Run-start fixtures:** cache the creature-candidate query across runs, keyed by the world database's content hash, to bring the fixture phase to ≤ 10 s.
- `release_runtime_checks.py` dev-only list: extending it with the five test-tooling modules is reviewed and accepted (they're harness-only, never in a release payload).

Go for tasks 2.x onwards, re-measuring each layer as it lands.

## 15. Part 3: closing the gap (orchestrator, 2026-10-11)

Part 2 measured 58.3 min of integration and journeys against 30. The replay of real PRs under the part-2 selector is
worse than the complete suite suggests, because of selection costs that have nothing to do with the change's own
tests. Replayed with the part-2 selector and per-check times from the `3c95011` full run:
- front-menu-saves (#33): integration 20.8 min, journeys 11.4 min, **gate 36.4 min** (the harness/app paths select
  `app.shim_perf` and the release checks), and the smoke set too, because the change edits `harness/active-changes`;
- the same change's product paths only: 13.0 + 10.9 min (+ 5.4 gate);
- m4-dungeon-scaling (#3) and true-pause (#32): FULL (`build.py` and a core patch). That's correct, which is why the
  complete suite has to fit 30 min, not just the per-PR selection.
- The smoke set costs 7.2 min (23 tests; `journey.first_play` 229 s of it).

Decisions (spec deltas edited in this commit; spec-change log 2026-10-11):
1. **Gate checks never run in a `pr` run.** They run in full runs (milestone gates and release tags). A pr run lists
   the gate checks its paths select as "owed". A change whose deltas add or change a gate check runs that check alone
   (`--only`) during verification. This replaces the "selected gates run in pr" rule. The builder brief and the spec
   disagreed; the brief was right.
2. **`harness/active-changes` is no-impact.** Every change edits it.
3. **The journey cap is 14 min (840 s).** The six budgets add up to 810 s. The old 12 min cap contradicted them, so
   `report.journeys` could never pass.
4. **`journey.first_play` is the `./play` first play.** `journey.app_desktop` is the app's cold open, Dock and quit.
   The spec text now says what was built.

Part 3 targets (measured, a miss is a failure):

| Layer / area | Part 2 (s) | Target (s) |
|---|---:|---:|
| Integration, whole layer | 2321 | ≤ 900 |
| Journeys, each | see 12.3 | ≤ its budget (sum 810) |
| Integration + journeys | 3497 | ≤ 1800 |
| Replay mean of the last 15 merged PRs (`report.pr_average` replay) | not measured | ≤ 900 |

The levers, in order of seconds per hour of work:
1. **No cold `./play` or real-client session in the integration layer (~1350 s today).** A scenario that needs the
   client is a child inside an existing journey session, sharing it, not a new session. A scenario that only needs the
   server is driven directly on the shared server. Applies to backups kill-safe/restore (517 s), play S1–S3 (379 s) and
   the client area's runs (195 s).
2. **Merged journey sessions (design 9.4).** One cold session for `first_play`, one session for `saves`, one GUI
   session for `app_desktop`: each scenario asserts inside the shared session instead of starting its own.
3. **Saves (368 s) and dungeons (333 s).** Saves: `ListGen` and save validation in `SavesLogic`, pooled
   `load_ordering`. Dungeons: the `scale.solo_final_boss` read (191 s) inside a `dng` session.
4. **`PENDING_SLEEPS`:** the 69 reported waits become polls or get a spec citation.

Measurement: unit layer, then one run of every integration and journey check (not the gate layer, which is timed
apart and excluded from these budgets), then the replay. No full run by the builder; the verifier runs it.

