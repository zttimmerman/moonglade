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

## Offline proof: findings (task 2.1, builder B7, 2026-10-02)
sandbox-exec works for the whole loop. No `pf` and no root needed, so the fallback isn't used.

- **Profile** (written per run by `play.offline`, with a unique log tag):
  `(allow default)`, `(deny network-outbound (remote ip "*:*") (with message TAG))`, `(allow network-outbound (remote ip "localhost:*"))`, and `(deny network-outbound (literal "/private/var/run/mDNSResponder") (with message TAG-dns))`. `remote ip "*:*"` covers IPv4 and IPv6, and `localhost` covers 127.0.0.1 and ::1. The DNS-resolver deny goes beyond the spec: it shows the loop also works with no name resolution at all.
- **Inheritance:** the sandbox is inherited across fork/exec and `setsid`, so `start_new_session` children are covered too. While the stand-in runs, `sandbox_check(pid, NULL, 0)` (libSystem via ctypes) returns 1 for `./play`, mysqld, authserver, worldserver, and every Wine process of the prefix (wineserver, services/winedevice/plugplay/svchost, start.exe, cmd.exe). The check requires that.
- **The deny is live, not vacuous.** Under the same profile file, a direct TCP connect to 1.1.1.1:443 and to [2606:4700:4700::1111]:443 fails with EPERM, a connect to 127.0.0.1 gets ECONNREFUSED from a closed port (so loopback isn't blocked), and the kernel logs `Sandbox: Python(pid) deny(1) network-outbound remote:*:443` with the tag. Through Wine, `wine cmd /c ping -n 1 1.1.1.1` is denied and logged as `winedevice.exe(pid) deny(1) network-outbound remote:*:0`, because Wine's ICMP goes through nsiproxy in winedevice.exe.
- **Denial log:** `log show --start <t> --style ndjson --predicate 'eventMessage CONTAINS "TAG"'`. Kernel reports redact the address (`remote:*:443`) and fold repeats into "N duplicate report(s)", but the first report per process/operation is always there, with the process name and pid. The loop window starts after the self-tests and contains no IP denial.
- **Observed DNS attempt:** each Wine start runs `wineboot.exe`, which asks the system resolver once (denied: `wineboot.exe deny network-outbound /private/var/run/mDNSResponder`). It's most likely Wine resolving the Mac's own hostname for the Windows computer name. That's a local IPC to mDNSResponder, not a non-loopback connection, and the loop works with it denied. The check records it (`loop_dns_lookups_denied`) but doesn't fail on it.
- **Consequence for process discovery:** `/bin/ps` is setuid root, and macOS refuses to exec a setuid binary from any sandboxed process (EPERM even under `(allow default)`). So `stack.stack_processes`, `stack.alive` and `launch.prefix_processes` now read the process table through `hslib/procs.py` (libproc + sysctl `KERN_PROCARGS2` via ctypes: the same data ps shows). `lsof` isn't setuid and works sandboxed. For every Wine process of the prefix, the output matches `ps -E -x -ww` exactly.
- **Not covered by sandbox-exec:** traffic that other system daemons make on the loop's behalf (mDNSResponder is denied here; nothing else was seen). Also, sandbox-exec is deprecated, though it still works on macOS 26 (Darwin 25.6). The `[human-verify]` Wi-Fi-off scenario remains the end-to-end proof with the real GUI client.

## Risks
- sandbox-exec is deprecated but still present on macOS. → Fallback above; the result is recorded in the change.
- Wine may spawn helper processes that outlive wineserver -k. → The cleanup checks look by prefix, as M0 does.
