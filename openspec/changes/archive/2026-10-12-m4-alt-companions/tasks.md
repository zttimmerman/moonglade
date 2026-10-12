Agent-hour estimates for builder work. This change is independent of the other two M4 changes and can be built while they verify (one stack owner at a time).

## 1. Prototype (riskiest assumption first)
- [x] 1.1 **Probe spike** (1.5–2 h):
  - the master's own second character comes online as a companion with `.playerbots bot add` while the master is logged in (one account, two characters online);
  - XP and loot land on the alt and persist after dismiss;
  - whether the addclass pool accounts are in playerbots' random-account list (`IsInRandomAccountList`), which decides the autogear approach (design.md §1);
  - what `autogear` does to replaced items.
  - Write findings into design.md.

## 2. Build
- [x] 2.1 **`MG_BRING_PLAN` and the `/mg bring` and `/mg dismiss` slash commands** (1.5 h).
- [x] 2.2 **Config `AutoGearCommandAltBots = 0`** (or the design.md §1 fallback) and profile `altgear-on` (0.5 h).

## 3. Checks
- [x] 3.1 **`alt.bring_dismiss`, `alt.foreign_refused`, `alt.gear_untouched`** (2–3 h): one shared game session (one login, one alt), each check once plus unit-level counterexamples.
- [x] 3.2 **`alt.progress_kept`** (2 h, uses front-menu-saves' load path), plus a unit-level counterexample (an alt-row change during the load must FAIL the comparison).
  - Built (a child of the `alt.bring_dismiss` session) with its unit counterexample. Both 2026-10-12 game runs failed its GIVEN: the second kill found no creature, because the first kill's corpse stayed selected and `.npc info <spawn>` reads the selection. Fixed (`kill_one` clears the selection; unit regression in `test_alt_checks.KillOne`). Not re-run live: the builder's two-run budget was spent. Its other rules held in both runs: XP 93→114 and 114→130, load `done`, alt rows unchanged. Live PASS on the fixed code in the PR-tier runs of PR #16 (3.2 s; XP 130→180, rows unchanged after the master's load).
- [x] 3.3 **Unit tests** (1 h): `unit:lua:test_addon_bring.bring_and_dismiss_send_the_command_lists`, `unit:py:test_alt_config.alt_gear_protected_in_every_profile`. (`client.bring_slash` and the `altgear-on` profile were cut; spec-change log 2026-10-12.)

## 4. Docs
- [x] 4.1 **Cheat sheet section, plus design "As built"** (0.5 h).

**Total builder estimate: ~8–9 agent-hours** (was 11–13 before the 2026-10-12 trim). Plus one verifier cycle (~1 h) and a full run.
