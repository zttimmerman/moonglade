# ADR-0003: Server map/DBC data from wowgaming/client-data for M0

- Status: accepted
- Date: 2026-10-01

## Context
worldserver needs DBC, maps, vmaps, and mmaps extracted from a 3.3.5a client. No client is on this machine yet (Zach supplies it for the M0 gate). AzerothCore's own tooling downloads pre-extracted data from `wowgaming/client-data` releases (v20.0, `Data.zip`, 1.1 GB).

## Decision
Use `client-data` v20.0 (URL pinned in `fork.lock`) so the server and harness work headless before the client exists. Re-extracting from Zach's own client is optional and can come later.

## Consequences
The server data might not match Zach's client exactly if their client has non-enUS/modified DBCs; that shows up at the M0 human gate.
