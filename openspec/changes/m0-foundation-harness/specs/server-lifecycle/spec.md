# Spec Delta: server-lifecycle

## Purpose
Start and stop the full server stack (MySQL, authserver, worldserver) headlessly, detect readiness, fail fast, and leave no orphaned processes.

## ADDED Requirements

### Requirement: Isolated runtime
The stack SHALL use only repo-local data and runtime directories, and SHALL bind every listener to 127.0.0.1. It SHALL NOT start, stop, or modify any MySQL instance or datadir outside the repo.

#### Scenario: MySQL instance is repo-local
- **GIVEN** the stack is up
- **WHEN** the harness queries the MySQL server's `datadir` and `port`
- **THEN** `datadir` is inside the repo, `port` is 3307, and every listening socket owned by the stack's processes is bound to 127.0.0.1
- Check: `lifecycle.isolated`

### Requirement: Start with readiness
`harness/hs up` SHALL start MySQL, authserver, and worldserver (initializing databases on first run), and return only when all are ready: MySQL accepts queries, authserver accepts TCP on 3724, and worldserver answers a GM command over SOAP. It SHALL fail with a nonzero exit and a log excerpt if readiness isn't reached within a bounded timeout.

#### Scenario: Cold start reaches ready
- **GIVEN** no stack processes are running
- **WHEN** `harness/hs up` runs
- **THEN** it exits 0 within the readiness timeout, and immediately afterwards a SOAP `.server info` succeeds and port 3724 accepts a connection
- Check: `lifecycle.up`

#### Scenario: Start is idempotent
- **GIVEN** the stack is already up
- **WHEN** `harness/hs up` runs again
- **THEN** it exits 0 without starting duplicate processes (one mysqld, one authserver, one worldserver for this repo)
- Check: `lifecycle.idempotent`

#### Scenario: Startup failure fails fast
- **GIVEN** worldserver is configured so that it cannot start (e.g. pointed at a nonexistent data directory)
- **WHEN** `harness/hs up` runs with that configuration
- **THEN** it exits nonzero well before the readiness timeout, prints an excerpt of the worldserver log that shows the cause, and leaves no worldserver process running
- Check: `lifecycle.failfast`

### Requirement: Clean shutdown
`harness/hs down` SHALL stop worldserver (gracefully, saving players), authserver, and MySQL, and SHALL leave no orphaned processes or stale pid files.

#### Scenario: Shutdown leaves nothing behind
- **GIVEN** the stack is up
- **WHEN** `harness/hs down` runs
- **THEN** it exits 0, no process from this repo's stack remains, ports 3307/3724/8085/7878 are free, and worldserver's log shows a graceful shutdown (not a kill)
- Check: `lifecycle.down`

#### Scenario: Data persists across restart
- **GIVEN** a test character whose level was changed via the harness
- **WHEN** the stack goes down and back up
- **THEN** the character's level is unchanged
- Check: `lifecycle.persist`
