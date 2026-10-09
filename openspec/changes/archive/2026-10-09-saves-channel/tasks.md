## 1. Listener (offline)
- [x] 1.1 `harness/hslib/savesctl.py`: listener, token parsing, nonce, fixed codes, size cap, loopback bind, logging without the nonce
- [x] 1.2 Wire it into `./play` and the app launch (start, hand off port and nonce, stop on exit, heal after SIGKILL)
- [x] 1.3 Checks `savesctl.listener`, `savesctl.hygiene`, `savesctl.test_actions_refused`
## 2. Glue sender
- [x] 2.1 `client-mods/glue`: control request (SetCVar realmList, DefaultServerLogin, read the reply, hide stock dialogs, reset realmList, timeout)
- [x] 2.2 Test actions `ctl:<verb>` and `realm:reset`; refused outside tests
## 3. Live
- [x] 3.1 First, a live run of the round trip. If A1 fails (realmList not settable, port ignored, reply unreadable), stop and report with evidence
- [x] 3.2 Checks `savesctl.live_roundtrip`, `savesctl.live_codes`, `savesctl.live_down`, `savesctl.realm_restored`; pick the accepted and refused codes from the table and record it in design.md
## 4. Docs
- [x] 4.1 design.md "As built"; append the live results to the spike doc's facts in this change's design (not the spike branch)
