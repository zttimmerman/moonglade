# Design: M0 Foundation

## Context
See proposal.md. Host constraints and base choices are in ADR-0001/0002/0003. Python 3.14 (stdlib only) and the Homebrew `mysql` client are on the host; no Python packages are installed, so the harness stays stdlib-only.

## Goals / Non-Goals
**Goals:** a deterministic, headless, single-entry harness; strong checks (protocol-level login, not "process started"); no state outside the repo.
**Non-Goals:** the `./play` launcher (M1), bot population (M2), any client binary changes, re-extracting maps from Zach's client.

## Decisions

**Layout**
```
harness/hs                 bash shim → python3 -m hslib (sets PYTHONPATH)
harness/hslib/
  paths.py                 repo-relative paths, ports, fork.lock parsing
  build.py                 vendor sync (pin + patches + module links), cmake configure/build/install
  stack.py                 mysqld / authserver / worldserver: init, up, down, status, pidfiles in env/run/
  config.py                render *.conf from *.conf.dist with key overrides (written to server/etc/)
  soap.py                  GM commands over SOAP (urllib, HTTP basic)
  db.py                    mysql CLI wrapper (--batch), assertion helpers → evidence dicts
  srp6.py                  SRP6 (salt/verifier for bootstrap GM account; client math for probe)
  probe/                   headless 3.3.5a client (auth + realm list + world session, ARC4 header crypt)
  fixtures.py              test account/character + state drivers
  clientcfg.py             realmlist.wtf / Config.wtf writer
  checks/                  one module per capability; @check("id") registry
  runner.py                hs run → results.json + summary.md
harness/results.schema.json
```

**Fork mechanics (ADR-0001).** `hs build` makes `vendor/azerothcore-wotlk` exactly `core_sha` (`git checkout -f`, `git clean -fd` outside `modules/`), applies `patches/core/*.patch` in lexical order with `git apply --index`, then symlinks `vendor/mod-playerbots` and each `modules/*` into `vendor/azerothcore-wotlk/modules/`. Same pin enforcement for mod-playerbots.

**Build.** cmake with Homebrew paths (`/opt/homebrew/opt/{mysql,openssl@3,readline}`), `CMAKE_CXX_COMPILER_LAUNCHER=ccache`, `TOOLS_BUILD=none`, `SCRIPTS=static`, `MODULES=static`, `CMAKE_BUILD_TYPE=RelWithDebInfo`, install prefix `server/`. Parallelism `-j8` (playerbots TUs are memory-heavy on 16 GB). `build.clean` builds in `build/` after wiping it. ccache keeps the clean build reasonably fast and is still a clean CMake tree.

**MySQL.** `mysqld --initialize-insecure --datadir=data/mysql` on first run, then `mysqld --defaults-file=env/my.cnf` (port 3307, bind 127.0.0.1, socket `env/run/mysql.sock`). The harness creates user `acore`@`localhost`/`127.0.0.1` with full privileges. Databases `acore_auth`, `acore_world`, `acore_characters` (+ the playerbots DB) are created and populated by AC's built-in updater (`Updates.AutoSetup=1`, `Updates.EnableDatabases=` all).

**Server config.** `config.py` copies `*.conf.dist` to `*.conf` and overrides keys: DB info strings → 127.0.0.1;3307, `DataDir` → `data/client-data`, `LogsDir` → `env/logs`, `SOAP.Enabled=1`, `SOAP.IP=127.0.0.1`, `SOAP.Port=7878`, `Console.Enable=0`, `BindIP=127.0.0.1`. The config is regenerated on every `up`, so it's never hand-edited.

**Bootstrap GM account.** SOAP needs a GM account before any GM command can create one, so `hs` inserts `hs_gm` into `acore_auth.account` with an SRP6 salt/verifier computed in `srp6.py`, plus an `account_access` row (gmlevel 3, RealmID -1). This is the only direct-insert account. The test account is made via SOAP `account create`, and the test character via the protocol probe's `CMSG_CHAR_CREATE`.

**Readiness.** MySQL: `SELECT 1` over the socket. Authserver: TCP connect to 3724. Worldserver: SOAP `.server info` returns 200. Poll every 1 s, timeout 600 s on first run (DB population) and 180 s otherwise. Fail-fast: if a launched pid exits while waiting, stop immediately and print the last 40 log lines.

**Shutdown.** SOAP `.server shutdown 1` → wait ≤60 s for exit; fallback SIGTERM (graceful in AC) → 30 s; last resort SIGKILL, which `lifecycle.down` treats as FAIL. Then authserver SIGTERM, then `mysqladmin shutdown`. Orphan check: no process whose executable is under `server/` or whose args reference this repo's `env/my.cnf`.

**Fixture state drivers.** All four go through SOAP GM commands as `HS_GM`; the DB is only read to observe what the server saved. Level: `.character level <name> <n>` (offline: the server runs `CHAR_UPD_LEVEL`). Teleport: `.teleport name <name> <game_tele name>` (offline: `Player::SavePositionInDB`). Item: `.send items <name> "subj" "text" <entry>` (mail; `item_instance` + `mail_items`). Quest: `.quest add <id> <name>` targets a named player from the console (cs_quest.cpp, `Console::Yes`), but its offline branch writes `character_queststatus.status = 1`, which is `QUEST_STATUS_COMPLETE` in QuestDef.h, not "incomplete". So the quest driver logs the character in with the protocol probe, sends `.quest add` while it is in the world (the online branch, `Player::AddQuestAndCheckCompletion`, which honours `CanAddQuest`), then logs out over the protocol so the server saves status 3 (`QUEST_STATUS_INCOMPLETE`). No playerbots path or spec change was needed. The level/teleport drivers require the character to be offline; the quest driver always logs out before returning. Fixture targets (Goldshire `game_tele` row, item entry, quest id) are looked up in the world DB at run time and recorded in check evidence.

**Checks and coverage.** Every check is `@check("id", capability=...)` and returns `{status, evidence}`. `report.coverage` parses all spec files for `#### Scenario:` blocks and requires each to carry `Check: \`id\`` with a registered id, or `[human-verify]` plus a reason.

## Risks / Trade-offs
- MySQL 9.2 vs AC's 8.0 baseline → fallback: Docker `mysql:8.4` on port 3307 (ADR-0002).
- Boost 1.92 newer than AC CI → fix via a core patch (with ADR) or pin older boost from source into `env/`.
- Disk: ~44 GB free; vendor 2.2 GB + build ~6 GB + client-data ~4 GB + DB ~2 GB. Zach's client (~17 GB) fits but is tight → summary prints free disk.
- Protocol probe complexity (SRP6 + ARC4 header crypt). Logon alone is a weaker proxy for "client connects", so the probe must reach `SMSG_LOGIN_VERIFY_WORLD`.
