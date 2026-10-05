# Proposal: Harness speed (GUI tier, faster full tier, F24)

## Why
Zach (2026-10-05): balance thorough checks with build/verify speed; waiting on redundant checks costs velocity. Three problems:
1. The launcher's focus-sensitive GUI checks now run in every full run, so every change needs a hands-off Mac for minutes and an unrelated stray click can fail it.
2. The full tier takes ~25–28 min, mostly checks that each start the real client (80–100 s) and restart servers.
3. F24 (P0 flake): `launch.prefix_processes()` counts any process with a file open under `env/wineprefix*` as ours, including macOS's StorageManagementService, which failed `client.launch_smoke` once.

## What Changes
1. **GUI tier.** Focus/GUI checks (app.single_dock_icon, app.handoff_focus, app.reopen_focuses_game, app.reopen_unminimizes, plus any other check needing exclusive screen focus) get a `gui` marker.
   - A full run skips them by design (recorded, like slow-tier skips), unless the change touches launcher/app/shim/GUI-check paths, in which case the full run includes them automatically.
   - `--tier gui` runs them alone. `report.full_run` accepts a by-design gui skip only when no such path changed.
2. **Faster full tier, nothing dropped.**
   - Profile the full tier per check, then cut wall time without removing or weakening any check: shared client sessions where scenarios allow, reuse a running server instead of restarting, ordering to avoid redundant ups/downs.
   - Target: at least 30% less wall time for an unfiltered full run on the same machine, measured before and after.
3. **F24.** `prefix_processes()` counts only this user's Wine/Wow processes (executable in the pinned runtime or the client/play client), not unrelated processes that merely hold a file open under the prefix.

## Capabilities
### Modified Capabilities
- `harness-reporting`: GUI tier rule, timing record, process-scope rule.

## Impact
Harness only. No server, client, module or app behaviour changes.

## Spec-change log
(none yet)
