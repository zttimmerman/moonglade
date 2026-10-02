# Tasks

## 1. Play loop (builder)
- [ ] 1.1 `./play` shim + `harness/hslib/play.py` (up → config → fixtures → launch → wait → down), `--client-cmd` test hook — verify: `play.loop_clean_exit`, `play.startup_failure`
- [ ] 1.2 Signal handling + client-crash path — verify: `play.interrupt_clean_exit`, `play.client_crash_cleanup`
- [ ] 1.3 Single-instance lock — verify: `play.single_instance`

## 2. Offline proof (builder)
- [ ] 2.1 sandbox-exec network-deny wrapper + denial-log parsing; fallback analysis — verify: `play.offline`

## 3. Verify
- [ ] 3.1 Verifier pass + full harness run
- [ ] 3.2 Human: Zach plays offline (Wi-Fi off)
