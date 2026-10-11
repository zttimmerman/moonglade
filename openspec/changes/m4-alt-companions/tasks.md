Agent-hour estimates for builder work. This change is independent of the other two M4 changes and can be built while they verify (one stack owner at a time).

## 1. Prototype (riskiest assumption first)
- [ ] 1.1 **Probe spike** (1.5–2 h):
  - the master's own second character comes online as a companion with `.playerbots bot add` while the master is logged in (one account, two characters online);
  - XP and loot land on the alt and persist after dismiss;
  - whether the addclass pool accounts are in playerbots' random-account list (`IsInRandomAccountList`), which decides the autogear approach (design.md §1);
  - what `autogear` does to replaced items.
  - Write findings into design.md.

## 2. Build
- [ ] 2.1 **`MG_BRING_PLAN` and the `/mg bring` and `/mg dismiss` slash commands** (1.5 h).
- [ ] 2.2 **Config `AutoGearCommandAltBots = 0`** (or the design.md §1 fallback) and profile `altgear-on` (0.5 h).

## 3. Checks
- [ ] 3.1 **`alt.bring_dismiss`, `alt.foreign_refused`** (2 h), ≥3× each plus counterexamples.
- [ ] 3.2 **`alt.progress_kept`** (2–3 h, uses front-menu-saves' load path), ≥3× plus a counterexample (an injected alt-row change during the load must FAIL it).
- [ ] 3.3 **`alt.gear_untouched`** (1.5 h), ≥3× plus the `altgear-on` counterexample.
- [ ] 3.4 **`client.bring_slash` GUI check** (1.5 h).

## 4. Docs
- [ ] 4.1 **Cheat sheet section, plus design "As built"** (0.5 h).

**Total builder estimate: ~11–13 agent-hours.** Plus one verifier cycle (~1 h) and a full run.
