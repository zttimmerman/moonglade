# Tasks

## 1. Play loop (builder)
- [x] 1.1 `./play` shim + `harness/hslib/play.py` (up → config → fixtures → launch → wait → down), `--client-cmd` test hook — verify: `play.loop_clean_exit`, `play.startup_failure`
- [x] 1.2 Signal handling + client-crash path — verify: `play.interrupt_clean_exit`, `play.client_crash_cleanup`
- [x] 1.3 Single-instance lock — verify: `play.single_instance`

## 2. Offline proof (builder)
- [x] 2.1 sandbox-exec network-deny wrapper + denial-log parsing; fallback analysis — verify: `play.offline`

## 3. Verify
- [x] 3.1 Verifier pass + full harness (cycle 1 FAIL: client.launch_smoke flaky (launcher counted as client) → fixed ef2d800; cycle 2 PASS, 3 full runs + 11 smoke runs, 2026-10-02) run
- [x] 3.2 Human: Zach plays offline (Wi-Fi off). Done 2026-10-02: `./play` exit 0, outcome client_exit, graceful halt, nothing left running
