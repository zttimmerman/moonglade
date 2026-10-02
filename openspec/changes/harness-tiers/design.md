# Design: harness tiers, smart clean build, instant logout

## 1. Tiers
- `@check(..., tier="quick"|"full"|"slow")`. Omitting `tier` still registers the check as `quick`, but it is
  recorded as undeclared (`tier_declared=False`), and `report.tiers_declared` fails for it. Every check therefore
  declares its tier explicitly.
- `hs run --tier T` runs the checks whose tier is T or an earlier tier in `(quick, full, slow)`. `--only` filters
  within that set. `results.json` `meta.tier` records T and is required by `results.schema.json`.
  `meta.full_run = unfiltered and T in {full, slow}`. On any other run `report.full_run` records `skip`
  ("not a full run (...)"). `runner.tolerated_skip` makes that one record, only on a non-full run, the sole
  non-pass status that doesn't fail the run. On a full run `report.full_run` is pass or fail, never skip, and a
  skip there would fail the run anyway. Any other check's skip fails every run.
- `hs list` shows each check's tier; `hs list --json` gives the machine-readable listing that
  `report.tiers_declared` checks.
- **Slow tier:** no check is `slow` today. Every scenario-referenced check has to run in a full run (a slow
  check named by a spec would make `report.full_run` fail), so `slow` is reserved for future soak checks that no
  scenario requires.

### `report.tiers` (post-run)
- **Unfiltered quick run:** it checks its own document: the executed set equals the registry's quick set, no
  full/slow check ran, `meta.tier == quick`, wall time < 180 s, all quick checks pass, and the real
  `report.full_run` record is `skip` with "not a full run".
- **Any other run that selects it** (every unfiltered full/slow run, or `--only` matching `report.tiers`): it runs
  `hs run --tier quick --results-dir harness/out/quick-tier` as a subprocess after the outer stack is down. It
  applies the same assertions to that document, uses the subprocess's wall time, and also requires the nested
  run's exit code to be 0 iff every check other than `report.full_run` passed. This keeps every full run self-contained: it proves the quick tier still works
  and fits its budget. The cost is one quick tier (about 1–1.5 min) added to the full run.

## 2. Smart clean build
- **Input hash:** sha256 over a JSON manifest of:
  - the server-build pins of fork.lock only: `core_repo/branch/sha` and `playerbots_repo/branch/sha`. The Ghidra,
    Wine runtime and data pins don't affect the server binaries, so bumping them doesn't force a from-empty build;
  - per-file content hashes of `patches/core/` and `modules/` (contents, not mtimes; symlinks by target;
    `.DS_Store` ignored);
  - `cmake_args()`, plus the resolved targets of the Homebrew `opt/` dependency links that those args name.
    `opt/boost` resolves to `Cellar/boost/<version>`, so a dependency upgrade counts as an argument change;
  - `clang`/`clang++`/`cmake --version`.
- **Stamp:** a passing from-empty build writes `{input_hash, manifest}` to `build/.hs-input-hash` and
  `server/bin/.hs-input-hash`.
- **Mode for `hs build --clean`:**
  - **from-empty:** chosen with `--force-clean`, when there's no build stamp, when the build stamp != the current
    hash, or when the install stamp != the current hash.
  - **verified-current:** chosen otherwise. It hashes the installed artifacts (`bin/*` minus the stamp, and
    `etc/**/*.dist`), runs the normal configure/build/install, and hashes them again. Any change fails the build
    and drops both stamps, so the next `--clean` builds from empty. A stamp != current hash also fails it.
- **Plain `hs build`:** an incremental build from inputs that no stamp vouches for drops the stamps. Example:
  edit a module, build incrementally, revert the edit. The binaries are now from unstamped inputs, so the next
  clean is from empty.
- **`build.clean`:** runs `hs build --clean` (with `--force-clean` under `hs run --force-clean`). It recomputes
  the input hash in-process, and in from-empty mode also checks that `build/` was really recreated
  (CMakeCache.txt inode changed) and that both stamps equal the hash.
- **`build.clean_smart`:**
  1. Unchanged inputs: it reuses this run's verified-current `build.clean` result, or runs `hs build --clean`.
     If that run had to build from empty, it runs it once more. It asserts the verified-current THEN.
  2. Changed inputs: it copies `modules/`, `patches/core/` and fork.lock into a temp sandbox, and requires the
     unmodified copy to hash to H. It then appends a byte to a file in the `modules/` and `patches/core/` copies,
     edits `core_sha`, `playerbots_sha` and `core_branch` in the fork.lock copy, and overrides the cmake args and
     the compiler identity in-process. Each case must give a hash != H and `decide_mode(...) == from-empty`.
     Editing unrelated pins (`ghidra_version`, `wowsilicon_runtime_sha256`) must leave the hash == H (still
     verified-current). The real inputs are digested before and after, and must be unchanged.

## 3. Instant logout for the test account
- AzerothCore checks `HasPermission(rbac::RBAC_PERM_INSTANT_LOGOUT)` (id 1) in
  `WorldSession::HandleLogoutRequestOpcode`. Permissions come from `auth.rbac_account_permissions`
  (`LOGIN_SEL_RBAC_ACCOUNT_PERMISSIONS`), merged with the defaults for the account's security level.
- The server's own way to grant one is the console GM command `.rbac account grant <account> <permId> [realmId]`
  (`cs_rbac.cpp`, `Console::Yes`). It calls `RBACData::GrantPermission`, which writes
  `(accountId, 1, granted=1, realmId=-1)` to that table. We use that command over SOAP rather than writing SQL,
  matching how the fixtures create accounts (`account create`).
  - Permission 1 has no `rbac_linked_permissions` children, so it grants nothing else.
  - `account_access` (the security level) is never touched, so the account stays level 0.
  - The table has `ON DELETE CASCADE` to `account`, so `account delete` (fixture reset) removes the grant.
- `fixtures.ensure_account()` grants it whenever the test account is created, and on existing test accounts if
  it's missing. It only ever names the `HSTEST` constant.
- `play.player_account_untouched` now includes the personal account's `rbac_account_permissions` rows in its
  before/after snapshot.
- **`fixture.instant_logout`:**
  1. Revokes the permission through the server, then has the fixture grant it again, so the grant path runs
     inside the check. Every other account's rbac and account_access rows must be unchanged.
  2. Logs the character in with the probe and logs out. It requires `SMSG_LOGOUT_RESPONSE` result 0 and
     instant=1, and request→`SMSG_LOGOUT_COMPLETE` < 5 s (the non-instant path waits the 20 s timer).
  3. Requires `characters.is_logout_resting = 0`, saved from `PLAYER_FLAGS_RESTING` at logout. This rules out a
     rest area as the reason the logout was instant.
  4. Requires the rbac rows to be exactly `[(1,1,-1)]`, no `account_access` row above 0, and the server's
     `.rbac account list` to show granted `[1]`, denied `[]`, at sec level 0.

## 4. Known core crash: server mail callback after an instant logout (workaround here)
- **Symptom (2026-10-02):** the worldserver segfaulted when the probe logged out within milliseconds of
  `SMSG_LOGIN_VERIFY_WORLD`, once instant logout was granted. Backtrace (macOS crash report):
  `Object::GetByteValue` ← `ServerMailCondition::CheckCondition` ← `ServerMailMgr::SendServerMail` ← the
  `ServerMailReward::OnPlayerLogin` query callback ← `WorldSession::ProcessQueryCallbacks`.
- **Cause:** `src/server/scripts/World/server_mail.cpp:29` (`ServerMailReward::OnPlayerLogin`) queues one async
  `CHAR_SEL_MAIL_SERVER_CHARACTER` per server mail. Each callback, when that mail was never received, calls
  `SendServerMail(session->GetPlayer(), ...)` (line 58) with no null check. After `LogoutPlayer` the
  session survives at character select with `GetPlayer() == nullptr`, so a callback that runs after the logout
  dereferences null. The 9 shipped `mail_server_template` mails have conditions (account flag 4) the test
  character never meets, so every login re-queries them. That opens the window on every login. The old 20 s
  logout timer hid it.
- **Workaround:** `fixtures.settle_login(ws, guid)` runs before every probe logout (fixtures,
  `client.protocol_probe`, `fixture.instant_logout`, `hs probe --login`).
  - `CharacterDatabase.WorkerThreads = 1` (asserted), so the character DB's async queue is FIFO.
  - The probe sends `CMSG_UPDATE_ACCOUNT_DATA` for per-character type 6, with the data unchanged and a new
    timestamp. The server queues `REPLACE INTO character_account_data` behind the login SELECTs.
  - When the DB shows the new timestamp, those SELECTs are done. A 0.25 s margin then lets a session update run
    their callbacks before the logout packet is handled.
  - This costs about 0.3 s per logout.
- **Why no core patch here:** core patches need their own ADR (CLAUDE.md §2). This change is harness-only, and a
  human client can't log out within milliseconds of login. The orchestrator plans an ADR'd core fix (null-check
  the player in the callback) before the M2 population work. Keep the workaround until that lands, and after,
  since it's harmless.
