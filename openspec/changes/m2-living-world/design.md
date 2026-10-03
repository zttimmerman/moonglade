# Design: M2 Living world

## Context
See proposal.md and `docs/research/playerbots-scripting.md` (sections 1, 2, 4 and 5) for what mod-playerbots can do headlessly.

## Decisions
- **Profiles in config.py.** `profile="m0"` keeps 10 bots for fast tests. `profile="population"` sets N random bots plus level/zone distribution and activity knobs (e.g. `BotActiveAlone`, SmartScale, teleport intervals). The builder picks the exact keys from `playerbots.conf.dist` and documents each one. Checks that need population bring the stack up with that profile; the runner orders them so that one warm-up serves `pop.*` and `perf.*`.
- **Zone mapping.** Read `AreaTable.dbc` (WDBC reader exists in `tools/era-diff/era_diff.py`; move it into `harness/hslib/dbc.py`). Starting zones and capitals are identified from DBC/`game_tele` data, with the selection rule written in the check; never from memory.
- **Probe chat.** Add CMSG_MESSAGECHAT (say/party/whisper) and SMSG_MESSAGECHAT parsing to `harness/hslib/probe/`, with opcodes and layouts taken from AC source. Companion checks run the probe as a GM-level account (playerbots grants GM senders full control).
- **Moving the master.** A GM teleport of the test character (`.tele name`) stands in for walking. Follow is measured by bot position (the `.pinfo`/`.gps`-style output over SOAP, or the probe's received movement updates; the builder chooses and documents).
- **World-update time.** Use the server's own metric: `.server info` reports update-time diffs in AC. If it doesn't expose a p95, sample it every 10 s and compute the statistics over the window.
- **Tuning record.** `docs/research/m2-population-tuning.md`: one row per measured N with peak and median metrics, picking the largest N within the budget.

## Notes from later work (2026-10-02)
- Upstream server-mail login crash is fixed by core patch 0001 (ADR-0006); bots logging in and out are safe from it.
- `fixtures.settle_login` requires `CharacterDatabase.WorkerThreads = 1`; if the population needs more DB workers, remove settle_login (core patch makes it non-essential) rather than weaken its barrier.
- Keep the Mac awake during long runs (F12): wrap warm-up/sampling in `caffeinate` or an IOPM assertion; a mysqld stall was seen during display sleep.
- Tag population/perf checks `slow`; companion checks can be `full` if they take < ~2 min.

## Risks
- Warm-up time grows full runs by about 10 minutes. → Acceptable; one shared warm-up.
- Bot behaviour is stochastic. → Thresholds use generous percentages and evidence keeps the raw samples; flakiness counts as P0, so tune the thresholds or knobs with recorded reasons.
- 16 GB is tight with the client running. → The budget reserves 3 GB host memory for the client; the human scenario confirms it.
