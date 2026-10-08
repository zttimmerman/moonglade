## Why

F34 (2026-10-07): the stock 67 W MagSafe can't keep up with full-load builds and test runs on the M2 Pro. While plugged in, the battery drained from 100 % (01:20) to 0 (~05:02, boot fault `vdd_hi_uvlo`), and the Mac rebooted at 05:05. Friends' laptops on small chargers will hit the same thing during `./bootstrap`. Today the only guard is a start-up refusal on battery power below 35 %; nothing caps build parallelism, and nothing pauses a long build or run that drains the battery while it is plugged in.

## What Changes

- **Build job cap.** `hs build` and `./bootstrap` pick a default job count from the Mac's performance cores (never all cores), and both take `--jobs N` to override. The chosen number and why appear in the build log.
- **Battery pause.** Heavy work (each build step, and between checks in a harness run) checks the battery. Below 35 %, **plugged in or not**, it pauses with one plain-language line and resumes on its own at 50 %. The pause is logged with start/end times. `--ignore-power` skips it.
- **Give up cleanly.** If the battery keeps falling while paused, plugged in or not, and reaches 15 %, the work stops with a plain message (what stopped, why, how to resume). Bootstrap resumes from that step on the next run, as it does today.
- The existing start-up refusal (battery power below 35 %) stays.

## Capabilities

### New Capabilities
- `power-guard`: cap build parallelism and pause or stop heavy work on low battery.

## Impact

harness/hslib (build, bootstrap, runner, guienv, a new `power` module), `./bootstrap`, cli. No game data, no client changes.

## Spec-change log
