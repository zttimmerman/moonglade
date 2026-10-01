# Tasks

## 1. Scaffolding (orchestrator)
- [x] 1.1 Repo structure, fork.lock, ADRs 0001-0003, STATUS.md — verify: files exist, committed
- [x] 1.2 `harness/hs` shim + `hslib` skeleton with check registry and `hs run` writing results.json/summary.md — verify: `hs run --only 'report.*'` writes both files

## 2. Build (orchestrator — glue)
- [x] 2.1 `hs build [--clean]`: vendor pin/patch/link + cmake build/install — verify: checks `build.clean`, `build.pin_enforced`

## 3. Lifecycle (orchestrator — glue)
- [x] 3.1 Isolated mysqld init/up/down — verify: `lifecycle.isolated`
- [x] 3.2 Config rendering, GM bootstrap account, auth/world up with readiness + fail-fast — verify: `lifecycle.up`, `lifecycle.idempotent`, `lifecycle.failfast`
- [x] 3.3 Graceful down + orphan detection — verify: `lifecycle.down`

## 4. GM + SQL (orchestrator)
- [x] 4.1 SOAP client and `hs gm` — verify: `gm.server_info`, `gm.invalid`
- [x] 4.2 `hs sql` + assertion helpers — verify: `sql.world_populated`, `sql.assert_selftest`
- [x] 4.3 Playerbots active check — verify: `build.playerbots_active`

## 5. Protocol probe + fixtures (builder B1)
- [x] 5.1 SRP6 logon + realm list for build 12340 — verify: `client.realmlist`, `client.protocol_badpass`
- [x] 5.2 World session (auth session, ARC4 header crypt), char enum, char create, player login → SMSG_LOGIN_VERIFY_WORLD — verify: `client.protocol_probe`
- [x] 5.3 Fixtures: test account (SOAP), test character (probe), level/teleport/item/quest drivers — verify: `fixture.*`, `lifecycle.persist`

## 6. Client config (builder B2)
- [x] 6.1 `hs client-config <dir>` — verify: `client.config_gen`

## 7. Reporting completeness (orchestrator)
- [x] 7.1 results.schema.json + validator; coverage self-check — verify: `report.schema`, `report.coverage`, `report.full_run`

## 8. Research (builder B3, non-blocking)
- [x] 8.1 mod-playerbots scriptability for behavioral tests → `docs/research/playerbots-scripting.md` — verify: report exists, cites source files/commands it tested

## 9. Verify + gate
- [x] 9.1 Verifier pass over all M0 scenarios (full `hs run`) — PASS required (cycle 1 FAIL on build.playerbots_active, fixed in fbad133; cycle 2 PASS, 3 full runs, 2026-10-01)
- [ ] 9.2 Human gate: Zach logs in under CrossOver and walks around
