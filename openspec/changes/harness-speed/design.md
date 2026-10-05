# Design: Harness speed
- GUI marker: a check attribute (like `tier="slow"`), plus a path rule list (e.g. `harness/app/`, `harness/hslib/app.py`, `harness/app/dockshim.m`, launcher-related `launch.py` functions, `harness/hslib/checks/polish_checks.py`). The changed-path set is computed from `git diff` against the merge-base with main (or origin/main) plus uncommitted changes.
- Timing: results.json already records per-check wall time. Add a `timing` summary (top N checks, total, by kind) and store the pre-change baseline from the last green full run (86 pass at 9265c01, ~1690 s) for comparison.
- Speed ideas to evaluate with data:
  - client-session sharing for login.* checks that each launch a fresh scratch play client;
  - keep the stack up across consecutive checks that need the same profile;
  - avoid repeated stack down/up between profile switches by ordering;
  - parallelise pure-Python checks that need no stack.
- Never loosen a check. Each check's assertions and evidence stay the same; only setup/teardown sharing changes, and each check still gets a clean precondition it verifies itself.
- F24: filter `prefix_processes()` by executable path/owner/Wine ancestry instead of "has an open file under the prefix".
