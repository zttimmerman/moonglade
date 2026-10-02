---
name: hs-harness-checks
description: How to write Hearthstone harness checks (harness/hslib/checks) that can actually fail. Use when adding or reviewing a @check, or mapping a spec scenario to a check.
---

# Writing harness checks

## Shape
```python
from ..registry import check, require
@check("cap.thing", order=NNN, capability="<capability>", stack="up"|"down"|"any")
def thing(ctx):
    """One line = the scenario's THEN, in plain words."""
    ev = {...}                      # commands, outputs, expected vs actual
    require(cond, "what was wrong", **ev)
    return ev                       # non-empty evidence is mandatory (report.schema)
```
- `order` sets the run sequence: destructive/"down" checks first, then lifecycle, then GM/SQL, fixtures, client, `lifecycle.down` (190), then client/RE checks (200+). Check `harness/hs list`.
- Prefer driving the real CLI through `subprocess` (`harness/hs …`) so the user-facing path is what gets tested.

## Make it able to fail
- Assert the THEN clause itself, not a proxy. Past verifier FAILs:
  - `build.playerbots_active` looked for "no such command", but AzerothCore says "Command '%s' does not exist", so it could never fail.
  - `client.inspect` computed unrecorded binaries but never required that list to be empty.
- For every new check, run a counterexample (sabotage a throwaway copy, or an in-process patch) and confirm FAIL, then restore.
- Match real formats from source or logs: Wine `err:d3d`, mtld3d `ERROR mtld3d::…` (ANSI-coloured), AzerothCore SOAP faults.
- Read logs only after reaping the process; keep stdout and stderr in separate files (buffering makes merged tails non-deterministic).

## Flakiness is P0
- Thresholds and timeouts get recorded reasons. Run the full harness twice before claiming green.
- Use generous, documented thresholds for stochastic behaviour (bots); keep raw samples in evidence.

## Coverage
- Every scenario: `Check: \`id\`` or `[human-verify]` plus a reason. Only changes listed in `harness/active-changes` (plus archived specs) gate the run.
