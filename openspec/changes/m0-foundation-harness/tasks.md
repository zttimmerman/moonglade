# Tasks

## 1. Scaffolding (orchestrator)
- [ ] 1.1 Repo structure, fork.lock, ADRs 0001-0003, STATUS.md — verify: files exist, committed
- [ ] 1.2 `harness/hs` shim + `hslib` skeleton with check registry and `hs run` writing results.json/summary.md — verify: `hs run --only 'report.*'` writes both files

## 2. Build (orchestrator — glue)
- [ ] 2.1 `hs build [--clean]`: vendor pin/patch/link + cmake build/install — verify: checks `build.clean`, `build.pin_enforced`

## 3. Lifecycle (orchestrator — glue)
- [ ] 3.1 Isolated mysqld init/up/down — verify: `lifecycle.isolated`
- [ ] 3.2 Config rendering, GM bootstrap account, auth/world up with readiness + fail-fast — verify: `lifecycle.up`, `lifecycle.idempotent`, `lifecycle.failfast`
- [ ] 3.3 Graceful down + orphan detection — verify: `lifecycle.down`

## 4. GM + SQL (orchestrator)
- [ ] 4.1 SOAP client and `hs gm` — verify: `gm.server_info`, `gm.invalid`
- [ ] 4.2 `hs sql` + assertion helpers — verify: `sql.world_populated`, `sql.assert_selftest`
- [ ] 4.3 Playerbots active check — verify: `build.playerbots_active`

## 5. Protocol probe + fixtures (builder B1)
- [ ] 5.1 SRP6 logon + realm list for build 12340 — verify: `client.realmlist`, `client.protocol_badpass`
- [ ] 5.2 World session (auth session, ARC4 header crypt), char enum, char create, player login → SMSG_LOGIN_VERIFY_WORLD — verify: `client.protocol_probe`
- [ ] 5.3 Fixtures: test account (SOAP), test character (probe), level/teleport/item/quest drivers — verify: `fixture.*`, `lifecycle.persist`

## 6. Client config (builder B2)
- [ ] 6.1 `hs client-config <dir>` — verify: `client.config_gen`

## 7. Reporting completeness (orchestrator)
- [ ] 7.1 results.schema.json + validator; coverage self-check — verify: `report.schema`, `report.coverage`, `report.full_run`

## 8. Research (builder B3, non-blocking)
- [ ] 8.1 mod-playerbots scriptability for behavioral tests → `docs/research/playerbots-scripting.md` — verify: report exists, cites source files/commands it tested

## 9. Verify + gate
- [ ] 9.1 Verifier pass over all M0 scenarios (full `hs run`) — PASS required
- [ ] 9.2 Human gate: Zach logs in under CrossOver and walks around
