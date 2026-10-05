# Design: Harness speed

## GUI tier (as built)
- `@check(..., gui=True)` marks a check that needs exclusive screen focus (registry `Check.gui`, shown by `hs list`).
  Marked: `app.single_dock_icon`, `app.handoff_focus`, `app.reopen_focuses_game`, `app.reopen_unminimizes`.
  - handoff_focus, reopen_focuses_game and reopen_unminimizes judge which app is frontmost; a click by the user (or
    any app activating itself) changes the answer, and reopen_focuses_game activates Finder itself.
  - single_dock_icon judges LaunchServices app types (Foreground/UIElement) at >= 10 Hz across the hand-off. It
    doesn't read the frontmost app, but it is the Dock half of the same hand-off measurement, only GUI paths can
    change its outcome, and its samplers are part of the focus steps below. Named by the proposal.
  - Not marked: app.loading_stages and app.shim_scope (stage events, process environments; no focus), play.dock_identity
    (lsappinfo types of a terminal run; no focus), the app/play lifecycle checks (they open windows, but nothing they
    assert depends on focus), app.shim_perf (slow tier already; CPU of the real client, judged with the display state).
- `runner.GUI_PATHS` (fnmatch on repo-relative paths): `harness/app/*`, `harness/hslib/app.py`, `harness/hslib/dockshim.py`,
  `harness/hslib/launch.py`, `harness/hslib/play.py`, `harness/hslib/stages.py`, `play`,
  `harness/hslib/checks/polish_checks.py`, `harness/hslib/checks/app_checks.py`. File level (not function level), so the
  rule errs towards running the GUI checks.
- `runner.changed_paths()`: `git diff --name-only --no-renames <merge-base HEAD origin/main (else main)>` (committed,
  staged and unstaged) plus untracked files. If git can't answer, the rule counts as "changed" (fail closed: GUI checks run).
- Full and slow tiers: with no changed GUI path, `runner.gui_tier_skips()` records every gui check as skip
  `"gui tier, not run in a full run (no GUI path changed)"` (listed in `meta.gui_tier_skipped`, rule in `meta.gui_rule`);
  otherwise they run like any check. Filtered full/slow runs apply the same rule (as the slow tier does); `--tier gui`
  runs exactly the gui checks (never a full run, no nested quick tier).
- `runner.is_gui_skip()` accepts a skip only if the check is gui-marked, the runner listed it, the message matches and
  the run-start rule found no changed GUI path. `report.full_run` re-evaluates the rule on the checkout when it judges
  the run; if a GUI path has changed by then, the GUI skips count as failing.
- The shared app session takes the focus steps (Dock/window samplers, waiting for the stand-in to be frontmost,
  Finder + reopen, minimise + reopen) only when a gui check asks for it (`_session(focus=True)`). Without gui checks the
  session never activates another app, so a full run doesn't take the user's focus. loading_stages and shim_scope read
  only evidence the session collects either way.
- `report.gui_tier_rule` (quick): synthetic registries through the runner's own functions (as report.slow_tier_rule),
  the path patterns, `changed_paths()` against a scratch git repo, and the live registry's gui set.

## Timing (as built)
- `meta.timing` in every results.json: `per_check_s`, `checks_sum_s`, `total_wall_s`, `top10`; summary.md lists the 5 slowest.
- `harness/timing-baseline.json`: the pre-change baseline copied from `harness/out/verify-lp2/full2.results.json`
  (launcher-polish verifier, cycle 2: 86 pass, wall 1697.7 s at 9265c01), with its executed ids.
- `report.timing` (post-run, last): the summary covers every record; on an unfiltered full-tier run every baseline-executed
  id runs again (minus by-design GUI skips) and wall <= 70 % of the baseline. Other runs: summary only (the baseline is a
  full-tier run; a slow-tier run isn't comparable).

## Speed-ups (as built; nothing dropped or weakened)
- Order: `play.player_account_untouched` 370 -> 151 and `client.launch_smoke` 210 -> 189, so both run while the stack is
  already up (before lifecycle.down) instead of each forcing an up and a down among the stack-down checks.
- Shared ./play runs in login_checks (same pattern as the launcher-polish sessions): login.auto_login + login.no_glue_errors
  share one Continue run (no_glue_errors' script; auto_login's Continue moves from 3 s to 20 s after the menu, inside the
  same 90 s limit); login.cancel_shows_list shares login.backdrop_choice's restart run (no Continue, screenshots at ~5 s and
  ~65 s; the restart only needs the first). Each check keeps its assertions; each run starts from "nothing running".
- Evaluated, not done: parallel stack-free checks (they add up to ~35 s; CPU contention with timing-limited checks);
  sharing runs between the play-launcher lifecycle checks (each scenario is its own lifecycle: clean exit, signals, crash,
  single instance, sandbox, player file); keeping the stack up across ./play runs (./play always stops the stack it
  serves; changing that is a product change).

## F24
`launch.prefix_processes()` keeps its three candidate rules (files under the prefix/server dir, exact WINEPREFIX, Wow.exe
command line) and counts a candidate only if `launch.process_scope()` says it is the runtime's: executable under the
pinned runtime (`paths.WINE_BIN.parent`), a descendant of such a process, or an unreadable executable (fail closed).
Check: `harness.prefix_scope` (stand-in `/bin/sleep` holding `system.reg` open vs `wine cmd /c ping`).
