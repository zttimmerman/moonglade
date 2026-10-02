# Design: core-server-mail-guard

## 1. The patch
`patches/core/0001-server-mail-guard-async-player.patch` (one file, `src/server/scripts/World/server_mail.cpp`): `OnPlayerLogin` captures the character's `ObjectGuid` in each async-lookup callback. The callback starts with `Player* player = session->GetPlayer(); if (!player || player->GetGUID() != guid) return;` and passes `player` to `SendServerMail`. It's applied by `hs build`'s existing queue (`git apply --index`). The patch changes the build-input hash, so the first `hs build --clean` after it is a from-empty build (3 min 24 s with ccache, 2026-10-02).

## 2. Checks
- `core.patch_applied` (quick): applies the patch to core_sha in a throwaway git index (`GIT_INDEX_FILE` in a temp dir), so the real vendor tree is never touched. It requires that the patch touches only server_mail.cpp and that the patched text has the guard (capture, `if (!player || player->GetGUID() != guid) return;` before `SendServerMail(player, …)`). It also requires the guard in the vendor source the build used, and that the install stamp records this patch's sha256. Finally it computes the input hash over scratch copies of `patches/core/` (one unchanged copy, one with an edited patch).
- `core.login_logout_race` (full): runs `hs probe --login Hstester --no-settle` 20 times. The new `--no-settle` option requests logout right after `SMSG_LOGIN_VERIFY_WORLD`. Then it checks the worldserver pid is unchanged and alive, `server info` answers over SOAP, no new `worldserver*.ips` exists in `~/Library/Logs/DiagnosticReports/`, and every attempt got `SMSG_LOGOUT_COMPLETE` (instant, result 0).
- `core.login_logout_race_baseline` (full): the same loop against the pinned core built without patches, in `baseline/` (gitignored):
  - `baseline/src` is a git worktree of the vendor core at core_sha, with the same module symlinks. Build and install go to `baseline/build` and `baseline/server`.
  - The check stops the real worldserver (mysqld and authserver keep running) and starts the baseline worldserver with its own confs, logs and pidfile, on the same DBs and ports.
  - It requires a crash within 20 attempts, and a crash report for that pid whose top frames name `ServerMailMgr::SendServerMail` or `ServerMailReward::OnPlayerLogin`.
  - Afterwards it restores the stack with `stack.up()` and requires that the `server/` tree digest is unchanged.
  - Runtime: the first baseline build is 13 min (no ccache hits, since its source and build paths differ). After that the check takes about 56 s: an incremental no-op build of about 10 s, a worldserver start of about 16 s, about 8 s of crash-report latency, and the restore.

## 3. Evidence (2026-10-02)
- Baseline: crash on attempt 1 (first run) and on attempts 1 and 2 in later runs. Report `worldserver-2026-10-02-101024.ips`, SIGSEGV at 0x58, frames: `Object::GetByteValue` ← `ServerMailCondition::CheckCondition` ← `ServerMailMgr::SendServerMail` ← `ServerMailReward::OnPlayerLogin(Player*)::'lambda'` ← `QueryCallback::InvokeIfReady` ← `WorldSession::Update`.
- Patched: 20/20 in the check, and 200/200 more no-settle cycles (24 s) with the same pid and no crash report.
- Counterexamples:
  - `core.login_logout_race` pointed at the baseline worldserver fails with "worldserver died or was replaced".
  - `core.login_logout_race_baseline` given a scratch copy of the patched binary fails with "survived 20 attempts".
  - `core.patch_applied` fails for a patch whose context doesn't match, a patch without the guard condition, and a hash function blind to `patches/`. The unpatched source shows all five guard problems.

## 4. Decision (task 2.3): keep `settle_login` as defense in depth
Evidence:
1. With the patch, the no-settle path survives 220 consecutive cycles.
2. The quick tier passes with `settle_login` bypassed (experiment: 27 pass, 0 fail; reverted afterwards). No other login-time race shows up in the fixture paths.
3. `settle_login` costs 0.28 s per fixture logout (3 uses per quick run).

**Kept**, because:
- Fixtures are state drivers, not race tests. Waiting until the server's login-time DB work has finished keeps their saves deterministic at negligible cost.
- The race itself is no longer hidden. `core.login_logout_race` deliberately skips the barrier on every full run, and the baseline check proves that loop catches the defect. `--no-settle` stays an explicit opt-out, and the default probe/fixture behaviour doesn't change.

**Revisit trigger:** `settle_login` requires `CharacterDatabase.WorkerThreads = 1` (FIFO barrier). If M2 wants more character-DB workers for the bot population, remove `settle_login` instead of weakening the barrier. Point 2 is the evidence that this is safe. No existing check was changed or loosened. Only the `settle_login` docstring was updated, to point at the patch.
