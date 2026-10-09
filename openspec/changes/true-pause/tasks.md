## 1. Prototype and ADR
- [ ] 1.1 Throwaway measurement before committing to the design: apply a minimal version of the core switch locally, rebuild, pause a probe fight, and measure what drifts and what the client shows on resume (screenshots). If design A doesn't hold, stop and report
- [ ] 1.2 ADR-0013 "World pause core patch" (orchestrator writes it; builder supplies the measured facts)
## 2. Server
- [ ] 2.1 `patches/core/0003-world-pause.patch` (core switch, Map branch, GameTime offset, 3 cooldown checks) wired into the patch queue
- [ ] 2.2 mod-moonglade `MoongladePause` (toggle, command, root and cancel cast, taxi refusal, resume resync, unpause on logout, config)
## 3. Client
- [ ] 3.1 Moonglade addon: Esc menu hooks, banner, unpause before Logout/Exit
## 4. Checks and docs
- [ ] 4.1 `pause.*` checks and `client.pause_menu`
- [ ] 4.2 design "As built"; docs/setup or player notes
