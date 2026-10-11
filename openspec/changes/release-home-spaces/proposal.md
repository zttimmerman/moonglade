## Why
The first real install of v0.1.0, on a friend's Mac, stopped at "Moonglade couldn't create your game world". The release keeps its data in `~/Library/Application Support/Moonglade`, and that path has a space. AzerothCore reads the servers' `-c` option as a filesystem path through boost::program_options, which rejects a path containing whitespace:

    the argument ('…/Application Support/Moonglade/etc/authserver.conf') for option '--config' is invalid

(authserver/Main.cpp:296, worldserver/Main.cpp:739). Every test HOME was a short path without spaces (`…/release-scratch/fl-<pid>/h`), so no check saw it.

## What changes
- `stack.conf_value`: when the config path has whitespace, the servers get it relative to their working directory (release: `HOME/run`, so `../etc/authserver.conf`). Process discovery accepts both forms.
- Every scratch release HOME in the release and first-launch checks has a space in its name (`h s`, `h2 s`, `home s`), so the whole release path runs with a spaced HOME from now on.

## Impact
- `harness/hslib/stack.py`, `harness/hslib/checks/release_runtime_checks.py`, `harness/hslib/checks/firstlaunch_checks.py`.
- Ships as v0.1.1.
- Upstream: AzerothCore's `-c` parsing is unchanged upstream (same `value<fs::path>`); a report upstream is a candidate for the F17 contributions list.

## Spec-change log
- 2026-10-11: added "A release HOME path with spaces works" (a tightening).
