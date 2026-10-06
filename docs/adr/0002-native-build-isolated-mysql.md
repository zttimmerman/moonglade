# ADR-0002: Native macOS build; repo-local isolated MySQL instance

> Project renamed to Moonglade on 2026-10-05 (openspec/changes/rename-moonglade); this record keeps the name it was written under.

- Status: accepted
- Date: 2026-10-01

## Context
Host: Apple Silicon, macOS 26.6, 16 GB RAM, 10 cores, ~44 GB free disk. Homebrew has cmake, mysql 9.2, openssl@3, readline; boost 1.92 installed via brew. Docker.app is installed but the daemon was not running. Zach's Homebrew MySQL has an existing datadir (`/opt/homebrew/var/mysql`) holding user data.

## Decision
- Build authserver/worldserver natively (cmake + clang, Homebrew deps). Docker uses RAM on a 16 GB machine that bots will need, and it adds a VM network hop to the client path.
- Run a **separate** `mysqld` (Homebrew binary) with datadir `data/mysql/` inside the repo, bound to 127.0.0.1:3307, socket under `env/`. Never touch the Homebrew service or its datadir (§11: no overwriting user data outside the repo).
- Server ports: auth 3724, world 8085, SOAP 127.0.0.1:7878. All bound to localhost.

## Alternatives
Docker for MySQL only (fallback if MySQL 9.x proves incompatible with AC: use the `mysql:8.4` image, same port). Full Docker server: rejected for RAM and complexity.

## Consequences
MySQL 9.x is above AC's documented 8.0 minimum; incompatibility shows up as a harness failure, and the fallback is above.
