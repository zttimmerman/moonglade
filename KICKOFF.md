# Kickoff (remaining sections)

Standing rules moved to `CLAUDE.md`. Harness requirements (section 5) moved to `docs/harness-requirements.md`. Section numbers kept from the original.

> **Correction (2026-10-01, from Zach):** the target is WotLK 3.3.5a (build 12340), all content through level 80. "Pre-2.3" and "era profile" below mean adjusting **1–60 pacing and difficulty only** on top of WotLK. The VMaNGOS diff is limited to entities that exist in both DBs and fall in the 1–60 scope. It never deletes or replaces TBC/WotLK content or systems (Outland, Northrend, Death Knights, and so on). VMaNGOS is a read-only data reference. See CLAUDE.md §1, §2, §7.

## 3. Open decision (Gate 1)

**Era profile scope.** Present the human with a short decision memo: what the pre-2.3 profile would change (XP curve, quest XP, outdoor elite flags, mount levels, class mount quests, anything else you find), the effort and risk of each, and whether it should be a toggle or the default. Do not build M3 until this gate is signed.

## 8. Milestones and exit criteria

Each milestone is done only when all of its scenarios pass in the harness and its human gate (if any) is signed.

- **M0 Foundation:** server builds and boots on macOS, harness complete (section 5), client connects under CrossOver to localhost. *Human gate: Zach logs in and walks around.*
- **M1 Offline loop:** a single `./play` command starts the DB, auth, and world, waits for readiness, launches the client pointed at localhost, and on client exit shuts everything down cleanly with no orphaned processes. Works with no network connection.
- **M2 Living world:** world population bots active in starting zones and cities, party companion bots usable for questing. Performance budget defined and met on the target Mac (define it in the spec).
- **M3 Era profile:** per the Gate 1 decision, generated from the VMaNGOS diff, toggleable via config, with scenarios that spot-check representative entities (e.g. specific quest elites restored, XP to level at specific levels).
- **M4 Solo tuning:** dungeon scaling for 1 player plus N bots, tuned so a listed set of dungeons is completable. *Human gate: Zach plays two dungeons.*
- **M5 Polish:** backlog of quality-of-life items Zach adds after M4.

## 13. First session

1. Set up the repo structure (section 12) and the AzerothCore fork.
2. Write the OpenSpec change for M0 (harness plus build plus boot) with scenarios.
3. Execute M0 through the work loop.
4. In parallel, prepare the Gate 1 decision memo using the VMaNGOS diff approach.
5. End at the M0 human gate with STATUS.md current and the Gate 1 memo ready.
