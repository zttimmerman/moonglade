# Design: M1 play loop

## Context
M0 provides `stack.up/down`, `launch.start_client`/`stop_prefix`, `fixtures`, and `clientcfg`. See proposal.md.

## Decisions
- **Composition, not new machinery.** `play.py` calls the existing functions. `./play` is a bash shim like `harness/hs`.
- **Waiting on the client.** Wine's `wine64-preloader Wow.exe` pid is the client. `play` polls it (same discovery as `launch.wow_processes`). When it exits, `stop_prefix()` runs (wineserver -k), then `stack.down()`.
- **Signals.** `play` traps SIGINT/SIGTERM: it stops the prefix, then the stack. It's idempotent, so a second signal during shutdown is ignored and doesn't double-kill.
- **Headless testability.** A test-only `--client-cmd` override replaces Wow.exe with a scripted stand-in (a Wine `cmd /c` that waits or exits on cue), so the loop's control flow is tested without the GUI. The real-client path is covered by `client.launch_smoke` (M0) and the human scenario.
- **Lock.** `env/run/play.lock` holds the pid, taken with `fcntl.flock`; a stale lock (dead pid) is reclaimed.
- **Offline proof.** Run the loop under a sandbox-exec profile that denies `network-outbound` except to localhost and logs denials. The harness runs it with the stand-in client. If sandbox-exec can't wrap Wine or MySQL, the fallback is a temporary `pf` anchor, which needs root; that would be a §11 question for Zach, so try sandbox-exec first.

## Risks
- sandbox-exec is deprecated but still present on macOS. → Fallback above; the result is recorded in the change.
- Wine may spawn helper processes that outlive wineserver -k. → The cleanup checks look by prefix, as M0 does.
